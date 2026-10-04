"""Run a privacy-safe retrieval quality check against a dedicated test database.

The corpus and queries are synthetic. The script refuses databases whose name does
not look like a test database, uses only the loopback embedding endpoint, and
removes every seeded row in a finally block. It never prints retrieved text.

Required: MEGABRAIN_IT_DATABASE_URL (database name must end in _test/_it).
Optional: MEGABRAIN_EMBEDDING_URL, MEGABRAIN_API_TOKEN_FILE.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from benchmark.onnx_embed import EMBEDDING_MODEL_VERSION
from retrieval.hybrid import EMBEDDING_DIM, HybridRetriever

EMBEDDING_MODEL = "bge-m3-int8-onnx"
DEFAULT_EMBED_URL = "http://127.0.0.1:4300/v1/internal/embeddings"


def _test_dsn() -> str:
    dsn = os.environ.get("MEGABRAIN_IT_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Set MEGABRAIN_IT_DATABASE_URL to a dedicated test database DSN.")
    database = conninfo_to_dict(dsn).get("dbname", "")
    if not re.search(r"(?:^|_)(?:test|it)(?:_\d+)?$", database.lower()):
        raise SystemExit("Refusing to run: database name must end in _test/_it (optionally with a numeric suffix).")
    return dsn


def _loopback_embed_url() -> str:
    url = os.environ.get("MEGABRAIN_EMBEDDING_URL", DEFAULT_EMBED_URL)
    parsed = urllib.parse.urlparse(url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = (parsed.hostname or "").lower() == "localhost"
    if parsed.scheme != "http" or not loopback or parsed.path != "/v1/internal/embeddings":
        raise SystemExit("Embedding URL must be the loopback /v1/internal/embeddings endpoint.")
    return url


def _embed(texts: list[str], url: str, token: str) -> tuple[str, list[list[float]]]:
    if not 1 <= len(texts) <= 8:
        raise ValueError("Loopback embedder accepts one to eight texts per batch.")
    request = urllib.request.Request(
        url,
        data=json.dumps({"texts": texts}).encode(),
        headers={"Content-Type": "application/json", **(
            {"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Loopback embedder request failed: {type(exc).__name__}") from exc
    vectors = body.get("vectors")
    version = body.get("model_version")
    if not isinstance(vectors, list) or len(vectors) != len(texts) or not version:
        raise RuntimeError("Embedder returned an invalid response shape.")
    if any(len(vector) != EMBEDDING_DIM for vector in vectors):
        raise RuntimeError("Embedder dimension differs from the retrieval index.")
    return version, vectors


def _embed_many(texts: list[str], url: str, token: str) -> tuple[str, list[list[float]]]:
    version = None
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 8):
        batch_version, batch = _embed(texts[start:start + 8], url, token)
        if version is not None and version != batch_version:
            raise RuntimeError("Embedder model version changed during the evaluation.")
        version = batch_version
        vectors.extend(batch)
    return str(version), vectors


def _item_text(kind: str, content: dict) -> str:
    fields = ("title", "summary", "text", "content", "situation", "lesson",
              "rationale", "reason", "cause", "effect", "outcome", "result",
              "recommendation", "action", "description")
    return " ".join([kind, *(str(content[key]) for key in fields if content.get(key))])


def _insert_event(cur, event_id: str, project_id: str, text: str, created_at: str) -> None:
    cur.execute(
        """INSERT INTO events
           (event_id, source, project_id, session_id, event_type, created_at, payload)
           VALUES (%s, 'retrieval-quality-eval', %s, %s, 'USER_MESSAGE', %s, %s)""",
        (event_id, project_id, f"eval-session-{project_id}", created_at,
         json.dumps({"text": text})),
    )


def _insert_item(cur, item_id: str, project_id: str, event_id: str, kind: str,
                 text: str, valid_from: str, valid_to: str | None = None,
                 supersedes_id: str | None = None) -> None:
    content = {"title": text, "text": text}
    cur.execute(
        """INSERT INTO memory_items
           (item_id, project_id, kind, valid_from, valid_to, supersedes_id,
            source_event_ids, confidence, extractor_type, extractor_version, content)
           VALUES (%s,%s,%s,%s,%s,%s,%s,0.95,'EXPLICIT','retrieval-quality-eval',%s)""",
        (item_id, project_id, kind, valid_from, valid_to, supersedes_id,
         [event_id], json.dumps(content)),
    )


def _insert_vectors(cur, table: str, id_column: str, rows: list[tuple[str, str, list[float], str]]) -> None:
    for row_id, project_id, vector, text in rows:
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        cur.execute(
            f"""INSERT INTO {table}
                ({id_column}, content_hash, model, model_version, dimension, embedding,
                 {('project_id,' if table == 'memory_embeddings' else '')} indexed_at)
                VALUES (%s,%s,%s,%s,%s,%s::vector,{('%s,' if table == 'memory_embeddings' else '')}now())""",
            ((row_id, content_hash, EMBEDDING_MODEL, EMBEDDING_MODEL_VERSION,
              EMBEDDING_DIM, "[" + ",".join(f"{x:.7f}" for x in vector) + "]", project_id)
             if table == "memory_embeddings" else
             (row_id, content_hash, EMBEDDING_MODEL, EMBEDDING_MODEL_VERSION,
              EMBEDDING_DIM, "[" + ",".join(f"{x:.7f}" for x in vector) + "]")),
        )


def _ranked_event_ids(result: dict) -> list[str]:
    """Collapse raw-event and canonical-item hits to one rank per source event."""
    seen: set[str] = set()
    ranked: list[str] = []
    for item in result.get("results", []):
        ids = item.get("source_event_ids") or [item.get("event_id")]
        for event_id in ids:
            if event_id and event_id not in seen:
                seen.add(event_id)
                ranked.append(event_id)
    return ranked


def _metrics(cases: list[dict]) -> dict:
    recalls, reciprocal_ranks, ndcgs = [], [], []
    leaks = 0
    for case in cases:
        ranked = case["ranked"]
        relevant = set(case.get("relevant", []))
        if relevant:
            found = [i for i, event_id in enumerate(ranked, start=1) if event_id in relevant]
            recalls.append(len(set(ranked[:5]) & relevant) / len(relevant))
            reciprocal_ranks.append(1 / found[0] if found else 0.0)
            dcg = sum(1 / math.log2(i + 1) for i, event_id in enumerate(ranked[:5], 1)
                      if event_id in relevant)
            ideal = sum(1 / math.log2(i + 1)
                        for i in range(1, min(len(relevant), 5) + 1))
            ndcgs.append(dcg / ideal if ideal else 0.0)
        if case.get("project_id"):
            leaks += sum(1 for project_id in case["result_projects"]
                         if project_id != case["project_id"])
    return {
        "queries_scored": len(recalls),
        "recall_at_5": round(sum(recalls) / len(recalls), 4) if recalls else 0,
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else 0,
        "ndcg_at_5": round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else 0,
        "project_leaks": leaks,
    }


def run() -> int:
    dsn = _test_dsn()
    from scripts.migrate import apply_migrations

    apply_migrations(dsn)
    embed_url = _loopback_embed_url()
    token_path = Path(os.environ.get(
        "MEGABRAIN_API_TOKEN_FILE", str(Path.home() / ".config/megabrain/token")))
    token = token_path.read_text().strip() if token_path.is_file() else ""
    cfg = conninfo_to_dict(dsn)
    project_a = f"rqevala_{uuid.uuid4().hex[:8]}"
    project_b = f"rqeveb_{uuid.uuid4().hex[:8]}"
    prefix = f"rqe_{uuid.uuid4().hex[:10]}"
    rows = [
        (f"{prefix}_exact", project_a,
         f"Incident {prefix.upper()}-ID-742: ONNX batch worker indexes missing vectors in groups of eight.",
         "2026-09-01T10:00:00Z"),
        (f"{prefix}_cache", project_a,
         "We moved session state from a per-process local cache to Redis because replicas disagreed.",
         "2026-09-02T10:00:00Z"),
        (f"{prefix}_old", project_a,
         "Decision: Qdrant was selected as the vector backend.", "2026-08-01T10:00:00Z"),
        (f"{prefix}_current", project_a,
         "Decision: PostgreSQL pgvector replaced Qdrant as the production vector backend.",
         "2026-09-15T10:00:00Z"),
        (f"{prefix}_failure", project_a,
         "Worker stalled after database restart because its connection pool did not reconnect.",
         "2026-09-03T10:00:00Z"),
        (f"{prefix}_decoy", project_a,
         "Routine deployment completed with no database or vector search changes.",
         "2026-09-04T10:00:00Z"),
        (f"{prefix}_private", project_b,
         f"Private project marker {prefix.upper()}-PRIVATE-991: payroll migration notes.",
         "2026-09-05T10:00:00Z"),
    ]
    item_old, item_current, item_failure = f"{prefix}_i_old", f"{prefix}_i_current", f"{prefix}_i_failure"
    items = [
        (item_old, project_a, rows[2][0], "DECISION", "Qdrant vector backend", rows[2][3], rows[3][3], None),
        (item_current, project_a, rows[3][0], "DECISION", "PostgreSQL pgvector production vector backend", rows[3][3], None, item_old),
        (item_failure, project_a, rows[4][0], "FAILURE_PATTERN", "Worker failed to reconnect its database pool after restart", rows[4][3], None, None),
    ]
    all_texts = [row[2] for row in rows] + [_item_text(kind, {"title": text, "text": text})
                                                for _, _, _, kind, text, *_ in items]
    model_version, vectors = _embed_many(all_texts, embed_url, token)
    if model_version != EMBEDDING_MODEL_VERSION:
        raise RuntimeError("Loopback embedder model version does not match the retrieval index.")

    inserted = False
    retriever = HybridRetriever({"postgres_dsn": dsn, "retrieval_vector_candidates": 100})
    # Reuse the API process's ONNX session; never load a second local model.
    vector_iter = iter(vectors)
    retriever._encode_query = lambda query: _embed([query], embed_url, token)[1][0]
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_database(), EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")
            db_name, has_vector = cur.fetchone()
            if db_name != cfg.get("dbname") or not has_vector:
                raise RuntimeError("The selected test DB is missing pgvector or changed unexpectedly.")
            cur.execute("SELECT 1 FROM schema_migrations ORDER BY version DESC LIMIT 1")
            if cur.fetchone() is None:
                raise RuntimeError("Test database migrations are not applied.")
            cur.execute("INSERT INTO projects(project_id,name) VALUES (%s,%s),(%s,%s)",
                        (project_a, "Synthetic retrieval eval A", project_b, "Synthetic retrieval eval B"))
            for event_id, project_id, text, created_at in rows:
                _insert_event(cur, event_id, project_id, text, created_at)
            for item_id, project_id, event_id, kind, text, valid_from, valid_to, supersedes in items:
                _insert_item(cur, item_id, project_id, event_id, kind, text, valid_from, valid_to, supersedes)
            event_vectors = [(event_id, project_id, next(vector_iter), text)
                             for event_id, project_id, text, _ in rows]
            item_vectors = [(item_id, project_a, next(vector_iter),
                             _item_text(kind, {"title": text, "text": text}))
                            for item_id, _, _, kind, text, *_ in items]
            _insert_vectors(cur, "memory_embeddings", "event_id", event_vectors)
            _insert_vectors(cur, "memory_item_embeddings", "item_id", item_vectors)
        inserted = True

        queries = [
            ("exact_identifier", f"Incident {prefix.upper()}-ID-742", project_a, None,
             {rows[0][0]}),
            ("semantic_cache_migration", "Why did we stop keeping session state in each process?", project_a,
             None, {rows[1][0]}),
            ("current_decision", "Which vector store is the production system using now?", project_a,
             None, {rows[3][0]}),
            ("historical_decision", "Which vector backend had been selected?", project_a,
             "2026-08-15T00:00:00Z", {rows[2][0]}),
            ("failure_root_cause", "Why did the worker remain stuck after the database restarted?", project_a,
             None, {rows[4][0]}),
            ("project_isolation", f"Find marker {prefix.upper()}-PRIVATE-991", project_a,
             None, set()),
        ]
        evaluated = []
        latencies = []
        vector_degraded = 0
        for name, query, project_id, at_time, relevant in queries:
            result = retriever.search(query, mode="WARM", limit=10, project_id=project_id, at_time=at_time)
            ranked = _ranked_event_ids(result)
            latencies.append(result["latency_ms"])
            vector_degraded += int(result.get("vector_degraded", True))
            evaluated.append({
                "name": name, "ranked": ranked, "relevant": relevant,
                "project_id": project_id,
                "result_projects": [item.get("project_id") for item in result.get("results", [])],
            })
        metrics = _metrics(evaluated)
        metrics["vector_degraded_queries"] = vector_degraded
        metrics["latency_ms_p50"] = round(sorted(latencies)[len(latencies) // 2], 2)
        metrics["cases"] = [
            {"name": case["name"], "first_relevant_rank": next(
                (i for i, event_id in enumerate(case["ranked"], 1) if event_id in case["relevant"]), None)}
            for case in evaluated
        ]
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        # Project isolation and historical/current truth are hard correctness gates.
        hard_failures = metrics["project_leaks"] or vector_degraded
        if metrics["recall_at_5"] < 1.0:
            hard_failures += 1
        for case in evaluated:
            if case["name"] in {"current_decision", "historical_decision"}:
                if not case["ranked"] or case["ranked"][0] not in case["relevant"]:
                    hard_failures += 1
        if hard_failures:
            return 1
        return 0
    finally:
        retriever._drop_connection()
        if inserted:
            with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
                item_ids = [item[0] for item in items]
                event_ids = [row[0] for row in rows]
                cur.execute("DELETE FROM memory_item_embeddings WHERE item_id = ANY(%s)", (item_ids,))
                cur.execute("DELETE FROM memory_items WHERE item_id = ANY(%s)", (item_ids,))
                cur.execute("DELETE FROM memory_embeddings WHERE event_id = ANY(%s)", (event_ids,))
                cur.execute("DELETE FROM events WHERE event_id = ANY(%s)", (event_ids,))
                cur.execute("DELETE FROM projects WHERE project_id = ANY(%s)", ([project_a, project_b],))


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"retrieval quality eval failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
