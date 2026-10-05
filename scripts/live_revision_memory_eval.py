"""Read-only canary for revision-aware recall of zero-duration legacy items.

Samples explicit supersession pairs whose wall-clock validity interval is empty
but whose source-event revision interval is trustworthy. It never prints or
persists memory text, topics, project identifiers, or item identifiers.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import psycopg

from scripts.live_memory_quality_eval import _read_only_dsn
from scripts.live_temporal_memory_eval import QUERY_TEMPLATES, _percentile


def _sample(dsn: str, limit: int) -> list[dict]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
            raise RuntimeError("Database connection is not read-only.")
        rows = conn.execute(
            """SELECT old.item_id, new.item_id, old.project_id, old.kind,
                      old.content->>'item_key', old.valid_from_revision
                 FROM memory_items old
                 JOIN memory_items new ON new.supersedes_id=old.item_id
                WHERE old.valid_to IS NOT NULL AND new.valid_to IS NULL
                  AND old.extractor_type='EXPLICIT' AND new.extractor_type='EXPLICIT'
                  AND old.confidence >= 0.9 AND new.confidence >= 0.9
                  AND old.kind=new.kind AND old.kind=ANY(%s)
                  AND coalesce(old.content->>'status','') <> 'REJECTED'
                  AND coalesce(new.content->>'status','') NOT IN ('REJECTED','SUPERSEDED')
                  AND coalesce(old.content->>'content_status','') <> 'REJECTED_EMPTY'
                  AND coalesce(new.content->>'content_status','') <> 'REJECTED_EMPTY'
                  AND old.content->>'item_key' IS NOT NULL
                  AND old.content->>'item_key'=new.content->>'item_key'
                  AND old.valid_from >= old.valid_to
                  AND old.valid_from_revision IS NOT NULL
                  AND old.valid_to_revision > old.valid_from_revision
                  AND new.valid_from_revision=old.valid_to_revision
                ORDER BY md5(old.item_id || 'revision-recall-eval-v1')
                LIMIT %s""",
            (list(QUERY_TEMPLATES), limit),
        ).fetchall()
    return [{
        "old_id": old_id,
        "new_id": new_id,
        "project_id": project_id,
        "query": QUERY_TEMPLATES[kind].format(topic=topic),
        "at_revision": int(revision),
    } for old_id, new_id, project_id, kind, topic, revision in rows]


def _search(record: dict, token: str, *, at_revision: int | None = None) -> dict:
    body = {
        "query": record["query"],
        "project_id": record["project_id"],
        "mode": "WARM",
        "limit": 50,
    }
    if at_revision is not None:
        body["at_revision"] = at_revision
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


def _rank(results: list[dict], item_id: str) -> int | None:
    return next((rank for rank, hit in enumerate(results, 1)
                 if hit.get("memory_item_id") == item_id), None)


def run() -> int:
    dsn = _read_only_dsn()
    limit = min(100, max(10, int(os.environ.get("MEGABRAIN_REVISION_EVAL_PAIRS", "40"))))
    records = _sample(dsn, limit)
    if len(records) < 10:
        raise RuntimeError(f"Only {len(records)} eligible revision pairs; need at least 10.")

    token_path = Path(os.environ.get(
        "MEGABRAIN_API_TOKEN_FILE", str(Path.home() / ".config/megabrain/token")))
    token = token_path.read_text().strip() if token_path.is_file() else ""
    current_ranks: list[int | None] = []
    historical_ranks: list[int | None] = []
    current_ms: list[float] = []
    historical_ms: list[float] = []
    future_top5 = leaks = degraded = 0
    for record in records:
        current = _search(record, token)
        historical = _search(record, token, at_revision=record["at_revision"])
        current_results = current.get("results", [])
        historical_results = historical.get("results", [])
        current_ranks.append(_rank(current_results, record["new_id"]))
        historical_ranks.append(_rank(historical_results, record["old_id"]))
        current_ms.append(float(current.get("latency_ms", current["client_latency_ms"])))
        historical_ms.append(float(historical.get("latency_ms", historical["client_latency_ms"])))
        for result in (current, historical):
            degraded += int(result.get("vector_degraded", True))
            leaks += sum(hit.get("project_id") != record["project_id"]
                         for hit in result.get("results", []))
        future_top5 += int((_rank(historical_results, record["new_id"]) or 51) <= 5)

    count = len(records)
    output = {
        "evaluation": "read-only same-wall-time revision recall; diagnostic, not human-judged",
        "eligible_pairs": count,
        "current_item_hit_at_5": round(sum(r is not None and r <= 5 for r in current_ranks) / count, 4),
        "current_item_top_1": sum(r == 1 for r in current_ranks),
        "historical_old_item_hit_at_5": round(sum(r is not None and r <= 5 for r in historical_ranks) / count, 4),
        "historical_old_item_top_1": sum(r == 1 for r in historical_ranks),
        "future_item_in_historical_top_5": future_top5,
        "project_leaks": leaks,
        "vector_degraded_queries": degraded,
        "current_server_p50_p95_ms": [_percentile(current_ms, .50), _percentile(current_ms, .95)],
        "historical_revision_server_p50_p95_ms": [
            _percentile(historical_ms, .50), _percentile(historical_ms, .95)],
    }
    print(json.dumps(output, indent=2))
    return int(bool(leaks or degraded or future_top5)
               or output["current_item_hit_at_5"] < 1
               or output["historical_old_item_hit_at_5"] < 1)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"revision recall eval failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
