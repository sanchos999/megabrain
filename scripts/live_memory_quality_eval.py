"""Read-only retrieval evaluation against explicit current memories.

Questions are generated in-process from confirmed memory keys. No memory text,
queries, IDs, or project names are printed or persisted. PostgreSQL sessions are
forced read-only; embeddings use the loopback API's already-loaded model.

Required: MEGABRAIN_READONLY_DATABASE_URL (must select the `megabrain` DB).
Optional: MEGABRAIN_API_TOKEN_FILE, MEGABRAIN_EMBEDDING_URL, MEGABRAIN_EVAL_PER_KIND.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrieval.hybrid import HybridRetriever
from scripts.retrieval_quality_eval import _embed_many, _loopback_embed_url

QUESTIONS = {
    "DECISION": "Какое решение приняли по теме: {topic}?",
    "CONSTRAINT": "Какие ограничения нужно соблюдать для: {topic}?",
    "TASK": "Что нужно сделать по задаче: {topic}?",
}


def _read_only_dsn() -> str:
    dsn = os.environ.get("MEGABRAIN_READONLY_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Set MEGABRAIN_READONLY_DATABASE_URL explicitly.")
    params = conninfo_to_dict(dsn)
    if params.get("dbname") != "megabrain":
        raise SystemExit("Refusing to run: this evaluator is restricted to the megabrain database.")
    params["options"] = "-c default_transaction_read_only=on"
    return make_conninfo(**params)


def _sample(dsn: str, per_kind: int) -> list[dict]:
    selected: list[dict] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
            raise RuntimeError("Database connection is not read-only.")
        for kind in QUESTIONS:
            rows = conn.execute(
                """SELECT mi.item_id, mi.project_id, mi.kind, mi.content,
                          mi.source_event_ids, mi.confidence, src.payload->>'text'
                     FROM memory_items mi
                     LEFT JOIN events src
                       ON src.event_id = mi.source_event_ids[1]
                      AND src.project_id = mi.project_id
                    WHERE mi.kind=%s AND mi.extractor_type='EXPLICIT' AND mi.valid_to IS NULL
                      AND mi.confidence >= 0.9
                      AND COALESCE(mi.content->>'status','') NOT IN ('REJECTED','SUPERSEDED')
                    ORDER BY md5(mi.item_id || 'live-retrieval-eval-v2')
                    LIMIT %s""",
                (kind, per_kind),
            ).fetchall()
            for item_id, project_id, item_kind, content, source_ids, confidence, source_text in rows:
                content = content if isinstance(content, dict) else json.loads(content or "{}")
                topic = str(content.get("item_key") or "").strip()
                if len(topic) < 3:
                    topic = str(content.get("title") or "").strip()
                if len(topic) < 3:
                    text = str(content.get("text") or content.get("content") or "").strip()
                    topic = " ".join(text.split()[:8])
                if len(topic) < 3:
                    continue
                question = QUESTIONS[item_kind].format(topic=topic[:220])
                selected.append({
                    "item_id": item_id,
                    "project_id": project_id,
                    "kind": item_kind,
                    "source_ids": set(source_ids or []),
                    "confidence": float(confidence),
                    "fts_query": topic[:220],
                    "question": question,
                    # The original user wording is a more realistic query than
                    # a question synthesized from the memory's own item key.
                    # It remains in memory only and is never printed or stored.
                    "source_query": str(source_text or "").strip()[:1000],
                })
    return selected


def _evaluate(retriever: HybridRetriever, records: list[dict], *, fts_key_only: bool = False) -> dict:
    ranks: dict[str, list[int]] = {kind: [] for kind in QUESTIONS}
    latencies: list[float] = []
    project_leaks = vector_degraded = 0
    for record in records:
        query = record["fts_query"] if fts_key_only else record["question"]
        result = retriever.search(query, mode="WARM", limit=50,
                                  project_id=record["project_id"])
        latencies.append(float(result["latency_ms"]))
        vector_degraded += int(result.get("vector_degraded", True))
        project_leaks += sum(1 for hit in result.get("results", [])
                             if hit.get("project_id") != record["project_id"])
        for rank, hit in enumerate(result.get("results", []), 1):
            source_match = bool(record["source_ids"] & set(hit.get("source_event_ids") or []))
            if hit.get("memory_item_id") == record["item_id"] or source_match:
                ranks[record["kind"]].append(rank)
                break

    rank_values = [rank for kind_ranks in ranks.values() for rank in kind_ranks]
    queries = len(records)
    sorted_latency = sorted(latencies)
    return {
        "hit_at_5": round(sum(rank <= 5 for rank in rank_values) / queries, 4),
        "hit_at_10": round(sum(rank <= 10 for rank in rank_values) / queries, 4),
        "hit_at_20": round(sum(rank <= 20 for rank in rank_values) / queries, 4),
        "hit_at_50": round(sum(rank <= 50 for rank in rank_values) / queries, 4),
        "mrr_at_10": round(sum(1 / rank for rank in rank_values) / queries, 4),
        "top_1": sum(rank == 1 for rank in rank_values),
        "project_leaks": project_leaks,
        "vector_degraded_queries": vector_degraded,
        "search_latency_ms_p50": round(statistics.median(sorted_latency), 2),
        "search_latency_ms_p95": round(sorted_latency[min(queries - 1, int(queries * 0.95))], 2),
        "by_kind": {
            kind: {
                "queries": sum(row["kind"] == kind for row in records),
                "hit_at_5": round(sum(rank <= 5 for rank in ranks[kind]) /
                                   max(1, sum(row["kind"] == kind for row in records)), 4),
                "mrr_at_10": round(sum(1 / rank for rank in ranks[kind]) /
                                    max(1, sum(row["kind"] == kind for row in records)), 4),
                "top_1": sum(rank == 1 for rank in ranks[kind]),
            }
            for kind in QUESTIONS
        },
    }


def _evaluate_api(records: list[dict], token: str, *, query_field: str = "question",
                  require_item_match: bool = False) -> dict:
    times: list[float] = []
    server_times: list[float] = []
    fast_http: list[float] = []
    full_http: list[float] = []
    fast_server: list[float] = []
    full_server: list[float] = []
    ranks: list[int] = []
    skipped_key = skipped_topic = degraded = project_leaks = 0
    retrieval_paths: dict[str, int] = {}
    for index, record in enumerate(records, 1):
        # Trailing whitespace is tokenizer-equivalent but defeats the exact
        # query-vector cache, so semantic fallback timings remain cache-cold.
        base_query = record[query_field]
        query = base_query + " " * index
        payload = json.dumps({
            "query": query, "mode": "WARM", "limit": 50,
            "project_id": record["project_id"],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            "http://127.0.0.1:4300/v1/memory/search", data=payload,
            headers=headers, method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        times.append((time.perf_counter() - started) * 1000)
        server_time = float(result.get("latency_ms", 0))
        server_times.append(server_time)
        path = str(result.get("retrieval_path", "unknown"))
        retrieval_paths[path] = retrieval_paths.get(path, 0) + 1
        if result.get("vector_skipped"):
            fast_http.append(times[-1])
            fast_server.append(server_time)
            skipped_key += int(result["vector_skipped"] == "exact_item_key")
            skipped_topic += int(result["vector_skipped"] == "exact_item_topic")
        else:
            full_http.append(times[-1])
            full_server.append(server_time)
        degraded += int(result.get("vector_degraded", True))
        project_leaks += sum(1 for hit in result.get("results", [])
                             if hit.get("project_id") != record["project_id"])
        for rank, hit in enumerate(result.get("results", []), 1):
            if require_item_match:
                if hit.get("memory_item_id") == record["item_id"]:
                    ranks.append(rank)
                    break
                continue
            source_match = bool(record["source_ids"] & set(hit.get("source_event_ids") or []))
            if hit.get("memory_item_id") == record["item_id"] or source_match:
                ranks.append(rank)
                break

    sorted_times = sorted(times)
    sorted_server = sorted(server_times)
    def p50(values: list[float]) -> float | None:
        return round(statistics.median(values), 2) if values else None

    return {
        "hit_at_5": round(sum(rank <= 5 for rank in ranks) / len(records), 4),
        "top_1": sum(rank == 1 for rank in ranks),
        "vector_skipped_exact_key": skipped_key,
        "vector_skipped_exact_topic": skipped_topic,
        "semantic_fallback_queries": len(records) - skipped_key - skipped_topic,
        "retrieval_paths": retrieval_paths,
        "vector_degraded_queries": degraded,
        "project_leaks": project_leaks,
        "http_p50_ms": round(statistics.median(sorted_times), 2),
        "http_p95_ms": round(sorted_times[min(len(records) - 1, int(len(records) * 0.95))], 2),
        "server_p50_ms": round(statistics.median(sorted_server), 2),
        "server_p95_ms": round(sorted_server[min(len(records) - 1, int(len(records) * 0.95))], 2),
        "fast_path_http_p50_ms": p50(fast_http),
        "semantic_fallback_http_p50_ms": p50(full_http),
        "fast_path_server_p50_ms": p50(fast_server),
        "semantic_fallback_server_p50_ms": p50(full_server),
    }


def _whitespace_cache_probe(records: list[dict], token: str) -> dict:
    cold, equivalent = [], []
    for index, record in enumerate(records[:20], 1):
        query = record["source_query"]
        times = []
        for formatted in (f"{query}\n", f" \t{query}  \n"):
            payload = json.dumps({
                "query": formatted, "mode": "WARM", "limit": 10,
                "project_id": record["project_id"],
            }).encode()
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                "http://127.0.0.1:4300/v1/memory/search", data=payload,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
            if not result.get("vector_skipped"):
                times.append(float(result.get("latency_ms", 0)))
        if len(times) == 2:
            cold.append(times[0])
            equivalent.append(times[1])

    def p50(values: list[float]) -> float | None:
        return round(statistics.median(values), 2) if values else None

    return {
        "paired_semantic_queries": len(cold),
        "first_format_server_p50_ms": p50(cold),
        "equivalent_whitespace_format_server_p50_ms": p50(equivalent),
    }


def run() -> int:
    dsn = _read_only_dsn()
    per_kind = min(200, max(10, int(os.environ.get("MEGABRAIN_EVAL_PER_KIND", "40"))))
    records = _sample(dsn, per_kind)
    if len(records) < 30:
        raise RuntimeError(f"Only {len(records)} eligible explicit memories; need at least 30.")

    embed_url = _loopback_embed_url()
    token_path = Path(os.environ.get(
        "MEGABRAIN_API_TOKEN_FILE", str(Path.home() / ".config/megabrain/token")))
    token = token_path.read_text().strip() if token_path.is_file() else ""
    questions = [record["question"] for record in records]
    _, vectors = _embed_many(questions, embed_url, token)
    vector_by_question = dict(zip(questions, vectors, strict=True))
    retriever = HybridRetriever({"postgres_dsn": dsn, "retrieval_query_cache_max": 0})
    try:
        retriever._encode_query = lambda _question: None
        fts_key_only_metrics = _evaluate(retriever, records, fts_key_only=True)
        retriever._encode_query = lambda _question: None
        fts_only_metrics = _evaluate(retriever, records)
        retriever._encode_query = lambda question: vector_by_question[question]
        metrics = _evaluate(retriever, records)
    finally:
        retriever._drop_connection()
    api_metrics = _evaluate_api(records, token)
    topic_api_metrics = _evaluate_api(records, token, query_field="fts_query")
    source_records = [record for record in records if len(record["source_query"]) >= 20]
    source_query_metrics = _evaluate_api(
        source_records, token, query_field="source_query", require_item_match=True)
    whitespace_cache_metrics = _whitespace_cache_probe(source_records, token)
    result = {
        "evaluation": "generated questions from explicit current memories; proxy, not human-judged",
        "queries": len(records),
        "fts_key_only_diagnostic": fts_key_only_metrics,
        "natural_query_fts_only_diagnostic": fts_only_metrics,
        "metrics": metrics,
        "production_api_metrics": api_metrics,
        "production_api_topic_metrics": topic_api_metrics,
        "source_message_canonical_item_metrics": {
            "evaluation": "original user wording; canonical memory item required; lexical overlap is not controlled",
            "queries": len(source_records),
            **source_query_metrics,
        },
        "whitespace_equivalent_cache_probe": whitespace_cache_metrics,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    unsafe = metrics["project_leaks"] or metrics["vector_degraded_queries"]
    return int(bool(unsafe))


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"live memory eval failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
