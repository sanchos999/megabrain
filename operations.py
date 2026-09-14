"""Durable worker heartbeats and operational status helpers."""
from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import psycopg

from core.config import load_config

_INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
HEARTBEAT_WARN_S = int(os.environ.get("MB_WORKER_HEARTBEAT_WARN_S", "120"))
HEARTBEAT_ERROR_S = int(os.environ.get("MB_WORKER_HEARTBEAT_ERROR_S", "600"))
# Valid wait reasons: a worker in these states with a backlog is NOT stuck.
WAIT_REASONS = {
    "WAITING_DEBOUNCE", "WAITING_BATCH", "WAITING_MAX_WAIT", "RATE_LIMIT_HOURLY",
    "RATE_LIMIT_DAILY", "TOKEN_BUDGET", "DAILY_BUDGET", "MONTHLY_BUDGET",
    "PRICE_GUARD", "BACKOFF", "PAUSED", "NO_MEANINGFUL_EVENTS",
}
ABS_MAX_WAIT_S = int(os.environ.get("MB_CONSOLIDATION_ABS_MAX_WAIT_S", "86400"))


class WorkerHeartbeat:
    def __init__(self, component: str, version: str = "0.1.2", dsn: str | None = None):
        self.component, self.version = component, version
        self.dsn = dsn or load_config()["postgres_dsn"]
        self.instance_id = _INSTANCE

    def update(self, *, state="RUNNING", processed_items=0, success=False, error=None, detail=None,
               reset_errors=True) -> bool:
        try:
            with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO worker_status
                    (component,instance_id,version,started_at,last_heartbeat_at,last_success_at,last_error_at,
                     last_error_class,consecutive_errors,processed_items,state,detail)
                    VALUES (%s,%s,%s,now(),now(),CASE WHEN %s THEN now() END,CASE WHEN %s THEN now() END,
                            %s,CASE WHEN %s THEN 1 ELSE 0 END,%s,%s,%s)
                    ON CONFLICT (component) DO UPDATE SET instance_id=EXCLUDED.instance_id,version=EXCLUDED.version,
                      last_heartbeat_at=now(),last_success_at=CASE WHEN %s THEN now() ELSE worker_status.last_success_at END,
                      last_error_at=CASE WHEN %s THEN now() ELSE worker_status.last_error_at END,
                      last_error_class=CASE WHEN %s THEN %s ELSE worker_status.last_error_class END,
                      consecutive_errors=CASE WHEN %s THEN worker_status.consecutive_errors+1 WHEN %s AND %s THEN 0 ELSE worker_status.consecutive_errors END,
                      processed_items=worker_status.processed_items+%s,state=%s,detail=%s""",
                    (self.component,self.instance_id,self.version,success,bool(error),type(error).__name__ if error else None,
                     bool(error),processed_items or 0,state,detail,success,bool(error),bool(error),type(error).__name__ if error else None,
                     bool(error),success,reset_errors,processed_items or 0,state,detail))
            return True
        except Exception:
            return False

    def error(self, error: BaseException, detail=None) -> bool:
        return self.update(state="DEGRADED", error=error, detail=detail)


def unit_state(unit: str) -> str:
    try:
        return subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=2).stdout.strip() or "inactive"
    except Exception:
        return "unknown"


def worker_rows(pg) -> dict:
    with pg.conn.cursor() as cur:
        cur.execute("SELECT component,instance_id,version,last_heartbeat_at,last_success_at,last_error_at,last_error_class,consecutive_errors,processed_items,state,detail FROM worker_status ORDER BY component")
        columns = [d.name for d in cur.description]
        now = time.time(); result = {}
        for row in cur.fetchall():
            item = dict(zip(columns, row)); age = max(0.0, now-item["last_heartbeat_at"].timestamp())
            item["heartbeat_age_s"] = round(age, 1)
            item["health"] = "ERROR" if age > HEARTBEAT_ERROR_S else "DEGRADED" if age > HEARTBEAT_WARN_S or item["state"] in {"DEGRADED", "ERROR"} else "OK"
            for key in ("last_heartbeat_at","last_success_at","last_error_at"):
                if item[key]: item[key] = item[key].isoformat()
            result[item.pop("component")] = item
        return result


def scheduler_backlog(pg) -> dict:
    """Queue + progress inputs for the watchdog (§8): backlog beyond the absolute
    max wait with no valid wait reason means STUCK, not IDLE."""
    with pg.conn.cursor() as cur:
        cur.execute("""SELECT count(*),COALESCE(sum(pending_event_count),0),
                              COALESCE(max(extract(epoch from (now()-first_dirty_at))),0),
                              COALESCE(bool_or(paused OR budget_paused OR rate_paused
                                  OR last_block_reason IN ('RATE_LIMIT_HOURLY','RATE_LIMIT_DAILY','TOKEN_BUDGET_DAILY','BUDGET')),false)
                       FROM consolidation_projects WHERE pending_event_count>0""")
        projects, pending, oldest_age_s, valid_wait = cur.fetchone()
        return {"dirty_projects": int(projects), "pending_events": int(pending),
                "oldest_pending_age_s": float(oldest_age_s),
                "overdue": bool(int(projects) and float(oldest_age_s) > ABS_MAX_WAIT_S and not valid_wait)}


def dead_letter_stats(path: str) -> dict:
    """DLQ classification with lifecycle states (OPEN/ACKNOWLEDGED/REPLAYED/RESOLVED)."""
    try:
        db_path = Path(path).expanduser()
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            has_state = bool(conn.execute("SELECT 1 FROM pragma_table_info('dead_letters') WHERE name='state'").fetchone())
            if not has_state:
                return _legacy_dead_letters(conn)
            now = time.time()
            rows = conn.execute("""SELECT state,reason,min(failed_at) first_seen,max(failed_at) last_seen,count(*) n
                                   FROM dead_letters GROUP BY state,reason""").fetchall()
            stats = {"total": 0, "open": 0, "acknowledged": 0, "replayed": 0, "resolved": 0,
                     "new_24h": 0, "new_7d": 0, "reasons": {}}
            for row in rows:
                n = int(row["n"]); stats["total"] += n
                key = row["state"].lower()
                stats[key if key in stats else "open"] += n
                stats["reasons"][f'{row["state"]}:{row["reason"]}'] = n
            stats["new_24h"] = int(conn.execute("SELECT count(*) FROM dead_letters WHERE failed_at>=? AND state IN ('OPEN','REPLAYED')", (now-86400,)).fetchone()[0])
            stats["new_7d"] = int(conn.execute("SELECT count(*) FROM dead_letters WHERE failed_at>=? AND state IN ('OPEN','REPLAYED')", (now-7*86400,)).fetchone()[0])
            return stats
    except Exception as error:
        return {"error": type(error).__name__}


def _legacy_dead_letters(conn) -> dict:
    now = time.time()
    total, first, last = conn.execute("SELECT count(*),min(failed_at),max(failed_at) FROM dead_letters").fetchone()
    reasons = dict(conn.execute("SELECT reason,count(*) FROM dead_letters GROUP BY reason").fetchall())
    return {"total": int(total or 0), "open": int(total or 0), "acknowledged": 0, "replayed": 0, "resolved": 0,
            "new_24h": int(conn.execute("SELECT count(*) FROM dead_letters WHERE failed_at>=?", (now-86400,)).fetchone()[0]),
            "new_7d": int(conn.execute("SELECT count(*) FROM dead_letters WHERE failed_at>=?", (now-7*86400,)).fetchone()[0]),
            "legacy_schema": True, "reasons": reasons}


def outbox_counts(path: str) -> dict:
    try:
        with sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True, timeout=2) as conn:
            rows = dict(conn.execute("SELECT status,count(*) FROM outbox GROUP BY status").fetchall())
            return {**{k: rows.get(k, 0) for k in ("pending", "sending", "failed")},
                    "dead_letter": conn.execute("SELECT count(*) FROM dead_letters").fetchone()[0]}
    except Exception as error:
        return {"error": type(error).__name__}
