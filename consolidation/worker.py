"""M5 consolidation worker: async LLM extraction of derived memory items.

Runs AFTER durable events are committed (never on the write path). Reads
meaningful events, asks Model Router (:4100, FIXED canonical glm-5.3, JSON
schema) to extract derived items, writes them via Postgres.add_derived_item with
provenance (source_event_ids, confidence, extractor=LLM, status=CANDIDATE).

Safety (M5 §15): the LLM never declares a confirmed fact — everything it emits
is DERIVED with provenance and CANDIDATE status; explicit user statements take
higher authority (they are already EXPLICIT DECISION/CONSTRAINT items).

Model (M5 §14): MEGABRAIN_CONSOLIDATION_MODEL=glm-5.3 (FIXED canonical, NOT
compression-auto / main-auto selector).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from core.config import load_config
from storage.pg import EXPERIENCE_KINDS, Postgres

ROUTER_URL = os.environ.get("MEGABRAIN_ROUTER_URL", "http://127.0.0.1:4100")
CONSOLIDATION_MODEL = os.environ.get("MEGABRAIN_CONSOLIDATION_MODEL", "glm-5.3")
BATCH = int(os.environ.get("MB_CONSOLIDATION_BATCH", "25"))
MEANINGFUL_TYPES = {
    "USER_MESSAGE", "ASSISTANT_MESSAGE", "ERROR", "TEST_RESULT",
    "DECISION", "CONSTRAINT", "TASK_UPDATE", "FILE_WRITE", "SHELL_RESULT",
    "TURN_COMPLETED",
}
# Cap chunk chars so the LLM prompt stays bounded.
MAX_CHUNK_CHARS = 12000


def _first_json_object(text: str):
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except ValueError:
                pass
        i += 1
    return None


def _extract_items(events: list[dict]) -> list[dict]:
    """Ask glm-5.3 (json_object) to extract derived items, citing real event ids."""
    ids = [e["event_id"] for e in events]
    lines = []
    for e in events:
        text = (e.get("payload") or {}).get("text") or (e.get("payload") or {}).get("content") or ""
        text = (text or "")[:600]
        lines.append(f"[{e['event_id'][:12]}] {e['event_type']}: {text}")
    prompt = (
        "Ты извлекаешь структурированную память из журнала событий проекта. "
        "Только по приведённым событиям, цитируй только реальные event_id. "
        "Верни JSON-объект: {\"items\":[{\"kind\":\"EXPERIENCE|PROCEDURE|"
        "FAILURE_PATTERN|REJECTED_APPROACH\",\"title\":\"...\","
        "\"situation\":\"...\",\"action\":\"...\",\"result\":\"...\","
        "\"lesson\":\"...\",\"confidence\":0.0,\"source_event_ids\":[\"<id>\"]}]}. "
        "Не выдумывай. Если ничего значимого нет, верни {\"items\":[]}.\n\n"
        "События:\n" + "\n".join(lines)
    )
    body = {
        "model": CONSOLIDATION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        ROUTER_URL + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = json.loads(resp.read())
    content = raw["choices"][0]["message"]["content"] or ""
    obj = _first_json_object(content)
    if obj is None:
        raise ValueError("no JSON in LLM output")
    items = obj.get("items") or []
    out = []
    for it in items:
        kind = (it.get("kind") or "").upper()
        if kind not in EXPERIENCE_KINDS:
            continue
        src = [s for s in (it.get("source_event_ids") or []) if s in ids]
        if not src:
            continue
        try:
            conf = float(it.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        out.append({
            "kind": kind,
            "content": {
                "title": it.get("title") or "",
                "situation": it.get("situation") or "",
                "action": it.get("action") or "",
                "result": it.get("result") or "",
                "lesson": it.get("lesson") or "",
                "status": "CANDIDATE",
            },
            "confidence": conf,
            "source_event_ids": src,
        })
    return out


class ConsolidationWorker:
    def __init__(self):
        self.cfg = load_config()
        self.pg = Postgres(self.cfg["postgres_dsn"], None, self.cfg["blob_inline_limit"])
        self.state_path = self.cfg.get("state_dir", "state") and os.path.join(
            __import__("pathlib").Path(__file__).resolve().parent.parent,
            "state", "consolidation-worker.json")

    def _load_cursor(self) -> str | None:
        try:
            with open(self.state_path) as f:
                return json.load(f).get("last_event_id")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_cursor(self, last_event_id: str, stats: dict):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"last_event_id": last_event_id,
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       **stats}, f)

    def _next_batch(self, cursor: str | None) -> list[dict]:
        with self.pg.conn.cursor() as cur:
            if cursor:
                cur.execute(
                    """SELECT event_id, project_id, session_id, event_type, created_at, payload
                       FROM events
                       WHERE event_type = ANY(%s)
                         AND (created_at, event_id) > (
                           SELECT created_at, event_id FROM events WHERE event_id=%s)
                       ORDER BY created_at, event_id
                       LIMIT %s""",
                    (list(MEANINGFUL_TYPES), cursor, BATCH))
            else:
                cur.execute(
                    """SELECT event_id, project_id, session_id, event_type, created_at, payload
                       FROM events
                       WHERE event_type = ANY(%s)
                       ORDER BY created_at, event_id
                       LIMIT %s""",
                    (list(MEANINGFUL_TYPES), BATCH))
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            self.pg.conn.commit()
        return [dict(zip(cols, r)) for r in rows]

    def _chunk(self, events: list[dict]) -> list[list[dict]]:
        chunks, cur_chunk, cur_len = [], [], 0
        for e in events:
            text = (e.get("payload") or {}).get("text") or (e.get("payload") or {}).get("content") or ""
            n = len(text or "")
            if cur_chunk and cur_len + n > MAX_CHUNK_CHARS:
                chunks.append(cur_chunk)
                cur_chunk, cur_len = [], 0
            cur_chunk.append(e)
            cur_len += n
        if cur_chunk:
            chunks.append(cur_chunk)
        return chunks

    def run_once(self) -> dict:
        cursor = self._load_cursor()
        events = self._next_batch(cursor)
        if not events:
            return {"status": "idle", "events": 0, "items": 0}
        stats = {"extracted": 0, "items_written": 0, "failed": 0, "events": len(events)}
        for chunk in self._chunk(events):
            try:
                items = _extract_items(chunk)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                self._log(f"extract failed: {str(e)[:160]}")
                continue
            stats["extracted"] += len(items)
            for it in items:
                project_id = chunk[0].get("project_id") or "unassigned"
                try:
                    self.pg.add_derived_item(
                        project_id, it["kind"], it["content"], it["source_event_ids"],
                        confidence=it["confidence"], extractor="LLM",
                        extractor_version=f"megabrain-m5:{CONSOLIDATION_MODEL}")
                    stats["items_written"] += 1
                except Exception as e:  # noqa: BLE001
                    stats["failed"] += 1
                    self._log(f"write failed: {str(e)[:160]}")
        last_id = events[-1]["event_id"]
        self._save_cursor(last_id, stats)
        return {"status": "ok", **stats}

    @staticmethod
    def _log(msg: str):
        print(f"[consolidation] {msg}", flush=True)
