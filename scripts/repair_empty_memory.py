"""Materialize safe evidence-backed candidates for invalid empty memory rows.

The original derived rows are retained with ``valid_to`` for auditability. The
repair creates low-confidence deterministic candidates; it never invents a
confirmed decision and can be re-run idempotently by item key.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from consolidation.worker import deterministic_candidate
from core.config import load_config
from storage.pg import BlobStore, Postgres


def repair(project_id: str | None = None) -> int:
    cfg = load_config()
    pg = Postgres(cfg["postgres_dsn"], BlobStore(cfg["blob_dir"]), cfg["blob_inline_limit"])
    with pg.conn.cursor() as cur:
        where = "content->>'status'='REJECTED_EMPTY'"
        args: list[str] = []
        if project_id:
            where += " and project_id=%s"
            args.append(project_id)
        cur.execute(f"""select item_id,project_id,kind,source_event_ids
                        from memory_items where {where} order by created_at,item_id""", args)
        bad = cur.fetchall()

    repaired = 0
    for item_id, pid, kind, source_ids in bad:
        with pg.conn.cursor() as cur:
            cur.execute("""select event_id,event_type,payload from events
                           where event_id = any(%s) order by created_at,event_id""",
                        (source_ids or [],))
            events = cur.fetchall()
        texts = []
        event_type = "ERROR" if kind == "FAILURE_PATTERN" else "ASSISTANT_MESSAGE"
        for event_id, et, payload in events:
            event_type = et or event_type
            payload = payload or {}
            text = str(payload.get("text") or payload.get("content") or "").strip()
            if text:
                texts.append(text[:1200])
        if not texts:
            continue
        candidate = deterministic_candidate({
            "event_id": f"repair:{item_id}",
            "event_type": event_type,
            "payload": {"text": "\n\n".join(texts)},
        })
        if candidate is None:
            continue
        content = dict(candidate["content"], item_key=f"repair:{item_id}")
        repaired_kind = kind if kind in {"EXPERIENCE", "PROCEDURE", "FAILURE_PATTERN", "REJECTED_APPROACH"} else candidate["kind"]
        with pg.conn.cursor() as cur:
            cur.execute("""select 1 from memory_items
                           where project_id=%s and kind=%s and valid_to is null
                             and content->>'item_key'=%s""",
                        (pid, repaired_kind, content["item_key"]))
            if cur.fetchone():
                continue
        pg.add_derived_item(
            pid, repaired_kind, content, list(source_ids or []),
            confidence=0.35, extractor="DETERMINISTIC",
            extractor_version="megabrain-0.2.0:empty-repair",
            status="CANDIDATE")
        repaired += 1
    return repaired


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project")
    args = parser.parse_args()
    print(f"repaired={repair(args.project)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
