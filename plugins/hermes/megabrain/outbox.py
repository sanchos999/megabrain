"""Local durable outbox for Hermes -> MegaBrain event delivery.

Section 3/4 of M5: Hermes must never lose events when MegaBrain is down, and
must never block a turn on a MegaBrain network round-trip.

Design:
  - SQLite in WAL mode (durable, concurrent-safe for single-writer).
  - append() commits BEFORE returning -> local ACK; p95 enqueue <2ms.
  - status lifecycle: pending -> sending -> delivered | failed.
  - failed rows retried later (attempts++, next_retry backoff).
  - idempotency: (event_id) UNIQUE — re-appending the same event is a no-op,
    so sender replay never duplicates at the MegaBrain side.

Pure stdlib (sqlite3). No megabrain imports.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    event_id      TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    created_at    REAL NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry    REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'   -- pending|sending|delivered|failed
);
CREATE INDEX IF NOT EXISTS idx_outbox_status_retry
    ON outbox(status, next_retry);
CREATE TABLE IF NOT EXISTS dead_letters (
    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, failed_at REAL NOT NULL,
    attempts INTEGER NOT NULL, last_error TEXT, reason TEXT NOT NULL, payload_hash TEXT NOT NULL
);
"""

# Retry backoff: base 2s, capped at 300s, deterministic per attempt count.
def backoff_seconds(attempts: int) -> float:
    return min(2.0 * (2 ** max(0, attempts - 1)), 300.0)


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(self, event: dict) -> bool:
        """Durable append. Returns True if inserted (new), False if duplicate.

        `event` must contain a stable `event_id` (str). The full dict is
        serialized as the payload for replay.
        """
        event_id = event.get("event_id")
        assert event_id, "event_id required"
        now = time.time()
        payload = json.dumps(event, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO outbox (event_id, payload, created_at, "
                "attempts, next_retry, status) VALUES (?,?,?,0,0,'pending')",
                (event_id, payload, now))
            self._conn.commit()
            return cur.rowcount == 1

    def dead_letter(self, event_id: str, *, last_error: str, reason: str) -> None:
        import hashlib
        with self._lock:
            row = self._conn.execute("SELECT payload,attempts FROM outbox WHERE event_id=?", (event_id,)).fetchone()
            if not row:
                return
            payload, attempts = row
            self._conn.execute("INSERT OR REPLACE INTO dead_letters(event_id,payload,failed_at,attempts,last_error,reason,payload_hash) VALUES(?,?,?,?,?,?,?)",
                               (event_id,payload,time.time(),attempts,last_error,reason,hashlib.sha256(payload.encode()).hexdigest()))
            self._conn.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
            self._conn.commit()

    def dead_letter_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM dead_letters").fetchone()[0]

    def dead_letters(self) -> list[dict]:
        with self._lock:
            return [dict(event_id=e,payload=p,failed_at=f,attempts=a,last_error=l,reason=r,payload_hash=h)
                    for e,p,f,a,l,r,h in self._conn.execute("SELECT event_id,payload,failed_at,attempts,last_error,reason,payload_hash FROM dead_letters ORDER BY failed_at")]

    def mark_sending(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET status='sending' WHERE event_id=? AND status='pending'",
                (event_id,))
            self._conn.commit()

    def mark_delivered(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM outbox WHERE event_id=?", (event_id,))
            self._conn.commit()

    def mark_failed(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET status='failed', attempts=attempts+1, "
                "next_retry=? WHERE event_id=?",
                (time.time() + backoff_seconds(self._attempts(event_id)), event_id))
            self._conn.commit()

    def _attempts(self, event_id: str) -> int:
        r = self._conn.execute(
            "SELECT attempts FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        return r[0] if r else 0

    def pending(self, limit: int = 100) -> list[dict]:
        """Events ready to (re)send: pending new OR failed past next_retry."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, payload FROM outbox "
                "WHERE (status='pending') OR (status='failed' AND next_retry <= ?) "
                "ORDER BY created_at ASC LIMIT ?", (now, limit)).fetchall()
            out = []
            for event_id, payload in rows:
                try:
                    out.append(json.loads(payload))
                except json.JSONDecodeError:
                    # corrupt row: drop it, never block the queue
                    self._conn.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
                    self._conn.commit()
            return out

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, count(*) FROM outbox GROUP BY status").fetchall()
            d = dict(rows)
            return {"pending": d.get("pending", 0), "sending": d.get("sending", 0),
                    "failed": d.get("failed", 0), "delivered": d.get("delivered", 0),
                    "total": sum(d.values())}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
