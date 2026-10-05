"""Privacy-safe temporal retrieval probe over superseded explicit memories.

Samples current/superseded item pairs in a forced read-only transaction, then
checks current-state and as-of retrieval through the local API. Query text,
memory text, project/item IDs, and timestamps are never printed or persisted.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

from scripts.live_memory_quality_eval import _read_only_dsn

QUERY_TEMPLATES = {
    "DECISION": "What did we decide about {topic}?",
    "CONSTRAINT": "Which constraints apply to {topic}?",
    "TASK": "What needs to be done for {topic}?",
}


def _interval_counts(dsn: str) -> tuple[int, int, int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
            raise RuntimeError("Database connection is not read-only.")
        return conn.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE old.valid_from < old.valid_to),
                      count(*) FILTER (WHERE old.valid_from >= old.valid_to)
                 FROM memory_items old
                 JOIN memory_items new ON new.supersedes_id=old.item_id
                WHERE old.valid_to IS NOT NULL AND new.valid_to IS NULL
                  AND old.extractor_type='EXPLICIT' AND new.extractor_type='EXPLICIT'
                  AND old.confidence >= 0.9 AND new.confidence >= 0.9
                  AND old.kind=new.kind AND old.kind IN ('DECISION','CONSTRAINT','TASK')
                  AND old.content->>'item_key' IS NOT NULL
                  AND old.content->>'item_key'=new.content->>'item_key'"""
        ).fetchone()


def _sample(dsn: str, limit: int) -> list[dict]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
            raise RuntimeError("Database connection is not read-only.")
        rows = conn.execute(
            """SELECT old.item_id, new.item_id, old.project_id, old.kind,
                      old.content->>'item_key', old.valid_from, old.valid_to
                 FROM memory_items old
                 JOIN memory_items new ON new.supersedes_id=old.item_id
                WHERE old.valid_to IS NOT NULL AND new.valid_to IS NULL
                  AND old.extractor_type='EXPLICIT' AND new.extractor_type='EXPLICIT'
                  AND old.confidence >= 0.9 AND new.confidence >= 0.9
                  AND old.kind=new.kind AND old.kind IN ('DECISION','CONSTRAINT','TASK')
                  AND old.content->>'item_key' IS NOT NULL
                  AND old.content->>'item_key'=new.content->>'item_key'
                  AND old.valid_from < old.valid_to
                  AND length(old.content->>'item_key') BETWEEN 4 AND 120
                ORDER BY md5(old.item_id || 'temporal-recall-eval-v1')
                LIMIT %s""",
            (limit,),
        ).fetchall()
    return [{
        "old_id": old_id,
        "new_id": new_id,
        "project_id": project_id,
        "kind": kind,
        "topic": topic,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "query": QUERY_TEMPLATES[kind].format(topic=topic),
        # Query one microsecond before the old item's end of validity.
        "at_time": (valid_to - timedelta(microseconds=1)).isoformat(),
    } for old_id, new_id, project_id, kind, topic, valid_from, valid_to in rows]


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 2)


def _search(record: dict, token: str, *, at_time: str | None = None) -> dict:
    body = {
        "query": record["query"],
        "project_id": record["project_id"],
        "mode": "WARM",
        "limit": 50,
    }
    if at_time:
        body["at_time"] = at_time
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        "http://127.0.0.1:4300/v1/memory/search",
        data=json.dumps(body).encode(), headers=headers, method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    result["client_latency_ms"] = (time.perf_counter() - started) * 1000
    return result


def run() -> int:
    dsn = _read_only_dsn()
    limit = min(100, max(10, int(os.environ.get("MEGABRAIN_TEMPORAL_EVAL_PAIRS", "40"))))
    eligible_pairs, positive_intervals, empty_intervals = _interval_counts(dsn)
    records = _sample(dsn, limit)
    if len(records) < 10:
        raise RuntimeError(f"Only {len(records)} eligible temporal pairs; need at least 10.")

    token_path = Path(os.environ.get(
        "MEGABRAIN_API_TOKEN_FILE", str(Path.home() / ".config/megabrain/token")))
    token = token_path.read_text().strip() if token_path.is_file() else ""
    current_hits: list[int | None] = []
    historical_hits: list[int | None] = []
    current_times: list[float] = []
    historical_times: list[float] = []
    current_paths: dict[str, int] = {}
    historical_paths: dict[str, int] = {}
    project_leaks = vector_degraded = temporal_violations = 0
    stale_current_top5 = future_item_historical_top5 = 0

    for record in records:
        current = _search(record, token)
        historical = _search(record, token, at_time=record["at_time"])
        current_times.append(float(current.get("latency_ms", current["client_latency_ms"])))
        historical_times.append(float(historical.get("latency_ms", historical["client_latency_ms"])))
        current_path = str(current.get("retrieval_path", "unknown"))
        historical_path = str(historical.get("retrieval_path", "unknown"))
        current_paths[current_path] = current_paths.get(current_path, 0) + 1
        historical_paths[historical_path] = historical_paths.get(historical_path, 0) + 1
        for result in (current, historical):
            vector_degraded += int(result.get("vector_degraded", True))
            project_leaks += sum(
                hit.get("project_id") != record["project_id"]
                for hit in result.get("results", [])
            )

        current_results = current.get("results", [])
        historical_results = historical.get("results", [])
        current_rank = next((rank for rank, hit in enumerate(current_results, 1)
                             if hit.get("memory_item_id") == record["new_id"]), None)
        old_current_rank = next((rank for rank, hit in enumerate(current_results, 1)
                                 if hit.get("memory_item_id") == record["old_id"]), None)
        historical_rank = next((rank for rank, hit in enumerate(historical_results, 1)
                                if hit.get("memory_item_id") == record["old_id"]), None)
        current_hits.append(current_rank)
        historical_hits.append(historical_rank)
        stale_current_top5 += int(old_current_rank is not None and old_current_rank <= 5)

        for hit in historical_results:
            item_id = hit.get("memory_item_id")
            if not item_id:
                continue
            valid_from = hit.get("valid_from")
            valid_to = hit.get("valid_to")
            at_time = datetime.fromisoformat(record["at_time"])
            parsed_from = datetime.fromisoformat(valid_from.replace("Z", "+00:00")) if valid_from else None
            parsed_to = datetime.fromisoformat(valid_to.replace("Z", "+00:00")) if valid_to else None
            if parsed_from and parsed_from > at_time:
                temporal_violations += 1
            if parsed_to and parsed_to <= at_time:
                temporal_violations += 1
            if item_id == record["new_id"] and hit.get("rank", 51) <= 5:
                future_item_historical_top5 += 1

    count = len(records)
    output = {
        "evaluation": "read-only superseded explicit pairs; diagnostic, not human-judged",
        "pairs": count,
        "eligible_supersession_pairs": eligible_pairs,
        "positive_validity_intervals": positive_intervals,
        "empty_or_negative_validity_intervals": empty_intervals,
        "current_item_hit_at_5": round(sum(rank is not None and rank <= 5
                                             for rank in current_hits) / count, 4),
        "current_item_top_1": sum(rank == 1 for rank in current_hits),
        "old_item_in_current_top_5": stale_current_top5,
        "historical_old_item_hit_at_5": round(sum(rank is not None and rank <= 5
                                                    for rank in historical_hits) / count, 4),
        "historical_old_item_top_1": sum(rank == 1 for rank in historical_hits),
        "future_item_in_historical_top_5": future_item_historical_top5,
        "historical_temporal_violations": temporal_violations,
        "project_leaks": project_leaks,
        "vector_degraded_queries": vector_degraded,
        "current_server_p50_p95_ms": [
            _percentile(current_times, 0.50), _percentile(current_times, 0.95)],
        "historical_server_p50_p95_ms": [
            _percentile(historical_times, 0.50), _percentile(historical_times, 0.95)],
        "current_retrieval_paths": current_paths,
        "historical_retrieval_paths": historical_paths,
    }
    print(json.dumps(output, indent=2))
    unsafe = project_leaks or vector_degraded or temporal_violations
    return int(bool(unsafe))


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"live temporal eval failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
