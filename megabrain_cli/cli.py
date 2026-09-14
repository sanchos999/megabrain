"""Public MegaBrain CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


def _base() -> str:
    return os.environ.get("MEGABRAIN_API_URL", f"http://{os.environ.get('MEGABRAIN_HOST', '127.0.0.1')}:{os.environ.get('MEGABRAIN_PORT', '4300')}")


def _outbox_path() -> Path:
    return Path(os.environ.get("MB_OUTBOX_PATH", "~/.hermes/megabrain-outbox.db")).expanduser()


def _get(path: str) -> dict:
    request = urllib.request.Request(_base() + path)
    token = os.environ.get("MEGABRAIN_API_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _consolidation(action: str) -> dict:
    import psycopg

    from consolidation.worker import ConsolidationWorker
    from core.config import load_config

    cfg = load_config()
    if action == "run --dry-run":
        try:
            return ConsolidationWorker().dry_run()
        except Exception as error:
            return {"status": "unavailable", "detail": str(error)[:200], "note": "Dry-run requires a functioning Router and DB"}
    try:
        with psycopg.connect(cfg["postgres_dsn"]) as conn, conn.cursor() as cur:
            if action in {"pause", "resume"}:
                value = "true" if action == "pause" else "false"
                cur.execute("INSERT INTO consolidation_settings(key,value) VALUES ('paused',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()", (value,))
                conn.commit()
                return {"status": "ok", "paused": action == "pause"}
            cur.execute("""SELECT count(*),COALESCE(sum(pending_event_count),0),min(first_dirty_at),
                                  min(next_eligible_at),max(retry_count),bool_or(budget_paused)
                           FROM consolidation_projects WHERE pending_event_count>0""")
            queue, pending, oldest, next_eligible, retries, budget_paused = cur.fetchone()
            cur.execute("SELECT count(*),COALESCE(sum(estimated_cost),0) FROM consolidation_runs WHERE started_at >= now()-interval '1 hour' AND status IN ('dispatching','success','failed')")
            calls_hour, cost_hour = cur.fetchone()
            cur.execute("SELECT count(*),COALESCE(sum(estimated_cost),0) FROM consolidation_runs WHERE started_at >= current_date AND status IN ('dispatching','success','failed')")
            calls_day, cost_day = cur.fetchone()
            cur.execute("""SELECT COALESCE(sum(COALESCE(tokens_in,tokens_in_estimated,0)),0),
                                  COALESCE(sum(tokens_in),0),COALESCE(sum(tokens_in_estimated),0),
                                  count(*) FILTER (WHERE tokens_in IS NULL AND tokens_in_estimated IS NULL),
                                  count(*) FILTER (WHERE cost_status='UNKNOWN'),count(*) FILTER (WHERE cost_status='REPORTED')
                           FROM consolidation_runs WHERE started_at >= current_date AND status IN ('dispatching','success')""")
            tokens_day, tokens_reported, tokens_estimated, tokens_unknown, cost_unknown, cost_reported = cur.fetchone()
            cur.execute("SELECT COALESCE(sum(estimated_cost),0) FROM consolidation_runs WHERE started_at >= date_trunc('month',now()) AND status IN ('dispatching','success','failed')")
            cost_month = float(cur.fetchone()[0])
            cur.execute("""SELECT model_selected,provider,count(*) FROM consolidation_runs
                           WHERE started_at >= now()-interval '7 days' AND model_selected IS NOT NULL GROUP BY 1,2 ORDER BY 3 DESC""")
            models = [dict(model=m or "?", provider=p or "?", calls=n) for m, p, n in cur.fetchall()]
            cur.execute("""SELECT finished_at,model_selected,provider,event_count,tokens_in,tokens_out,estimated_cost,status,error_class,
                                  tokens_in_status,tokens_out_status,cost_status,escalation_stage
                           FROM consolidation_runs ORDER BY finished_at DESC NULLS LAST LIMIT 1""")
            last = cur.fetchone()
            cur.execute("SELECT project_id,pending_event_count,first_dirty_at,last_block_reason FROM consolidation_projects WHERE pending_event_count>0 ORDER BY first_dirty_at LIMIT 10")
            dirty = [dict(zip(("project_id", "pending", "dirty_since", "block_reason"), row)) for row in cur.fetchall()]
    except Exception as error:
        return {"status": "unavailable", "worker": _worker_state(), "error": type(error).__name__, "detail": str(error)[:200]}
    return {"status": "ok", "worker": _worker_state(),
            "dirty_projects": queue, "pending_events": int(pending),
            "oldest_pending": oldest.isoformat() if oldest else None,
            "next_eligible_run": next_eligible.isoformat() if next_eligible else None,
            "current_backoff_retry": int(retries or 0), "calls_last_hour": int(calls_hour), "hourly_limit": 2, "hourly_remaining": max(0, 2-int(calls_hour)), "calls_today": int(calls_day), "daily_limit": 24, "daily_remaining": max(0, 24-int(calls_day)),
            "tokens_today": int(tokens_day), "tokens_reported": int(tokens_reported), "tokens_estimated": int(tokens_estimated),
            "tokens_unknown_calls": int(tokens_unknown), "token_daily_limit": int(os.environ.get("MB_CONSOLIDATION_MAX_INPUT_TOKENS_PER_DAY", "100000")),
            "cost_today": float(cost_day), "daily_cost_limit": 25.0, "cost_month": cost_month, "monthly_cost_limit": 250.0,
            "cost_unknown_calls": int(cost_unknown), "cost_reported_calls": int(cost_reported),
            "budget_state": "BUDGET_PAUSED" if budget_paused else "READY", "model_distribution": models,
            "dirty_detail": dirty,
            "last_consolidation": dict(zip(("finished_at", "model", "provider", "batch_size", "tokens_in", "tokens_out", "estimated_cost", "status", "error_class", "tokens_in_status", "tokens_out_status", "cost_status", "escalation_stage"), last)) if last else None}


def _worker_state() -> str:
    return subprocess.run(["systemctl", "--user", "is-active", "megabrain-consolidation-worker.service"], capture_output=True, text=True).stdout.strip()


# ---------- dead-letter lifecycle ----------

def _dlq(event_id: str | None, action: str | None, resolution: str) -> dict:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrations" / "hermes"))
    from outbox import Outbox  # vendored plugin copy

    box = Outbox(_outbox_path())
    try:
        if action == "stats":
            return box.dead_letter_stats()
        if action == "list":
            rows = box.dead_letters()
            show = [row for row in rows if not event_id or event_id in row["event_id"]][:50]
            return {"count": len(show), "items": show}
        if action == "acknowledge":
            if not event_id:
                return {"status": "error", "detail": "event_id required"}
            return {"status": "ok" if box.acknowledge(event_id, resolution) else "not_found"}
        if action == "replay":
            if not event_id:
                return {"status": "error", "detail": "event_id required"}
            return {"status": "ok" if box.replay(event_id) else "not_found"}
        return {"status": "error", "detail": "unknown action"}
    finally:
        box.close()


# ---------- doctor ----------

def _doctor() -> dict:
    """Functional checks only; no LLM calls, no destructive actions."""
    checks: dict[str, object] = {}
    try:
        import psycopg

        from core.config import load_config

        cfg = load_config()
        with psycopg.connect(cfg["postgres_dsn"], connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            checks["db_auth"] = "PASS"
            cur.execute("SELECT version,name FROM schema_migrations ORDER BY version DESC LIMIT 1")
            row = cur.fetchone()
            checks["migrations"] = f"PASS (v{row[0]} {row[1]})" if row else "FAIL"
            cur.execute("SELECT count(*) FROM memory_embeddings")
            checks["embeddings"] = int(cur.fetchone()[0])
            cur.execute("SELECT component,state,consecutive_errors FROM worker_status")
            checks["workers"] = {c: {"state": s, "errors": e} for c, s, e in cur.fetchall()}
        checks["redis"] = "PASS" if _get("/health").get("redis") else "FAIL"
    except Exception as error:
        checks["db_auth"] = f"FAIL ({type(error).__name__})"
    state = _get("/health/ops") if _get("/health/live").get("status") == "ok" else {"status": "unreachable"}
    checks["operational"] = state.get("status")
    checks["api"] = _get("/health/live").get("status")
    usage = shutil.disk_usage(Path.home())
    checks["disk_free_gb"] = round(usage.free / 2**30, 1)
    checks["disk"] = "PASS" if usage.free / usage.total > 0.1 else "WARN"
    model_dir = Path(os.environ.get("U2NET_HOME", "")) if os.environ.get("U2NET_HOME") else None
    checks["model_dir"] = ("PASS" if (model_dir and model_dir.exists()) else "UNKNOWN") if model_dir else "UNKNOWN"
    try:
        Path(os.environ.get("MB_STATE_DIR", str(Path(__file__).parent.parent / "state"))).mkdir(parents=True, exist_ok=True)
        checks["fs_writable"] = "PASS"
    except Exception as error:
        checks["fs_writable"] = f"FAIL ({type(error).__name__})"
    for unit in ("megabrain.service", "megabrain-embedding-worker.service", "megabrain-hermes-outbox.service", "megabrain-consolidation-worker.service"):
        checks[f"unit:{unit.split('megabrain-')[-1]}"] = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout.strip()
    overall = "PASS" if checks.get("db_auth") == "PASS" and checks.get("api") == "ok" and str(checks.get("migrations", "")).startswith("PASS") else "FAIL"
    return {"status": overall, "checks": checks}


def _status_screen() -> dict:
    live = _get("/health/live") if True else {}
    try:
        ops = _get("/health/ops")
    except Exception as error:
        ops = {"status": f"unreachable ({type(error).__name__})"}
    try:
        cons = _consolidation("status")
    except Exception as error:
        cons = {"status": f"unavailable ({type(error).__name__})"}
    dlq = _dlq(None, "stats", "")
    out = {"version": live.get("version"), "overall": ops.get("status"),
           "api": live.get("status"),
           "workers": {k: {"state": v.get("state"), "health": v.get("health"), "heartbeat_age_s": v.get("heartbeat_age_s"), "last_error_class": v.get("last_error_class")} for k, v in (ops.get("workers") or {}).items()},
           "services": ops.get("services"), "scheduler": ops.get("scheduler"),
           "dead_letters": dlq, "consolidation": {k: v for k, v in cons.items() if k in ("calls_last_hour", "calls_today", "tokens_today", "cost_today", "cost_unknown_calls", "tokens_unknown_calls", "last_consolidation", "dirty_projects", "pending_events", "model_distribution")},
           "outbox": ops.get("outbox")}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="megabrain")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, path in (("health", "/health"), ("status", "/health/ops"), ("projects", "/v1/projects")):
        sub.add_parser(name).set_defaults(path=path)
    cons = sub.add_parser("consolidation")
    actions = cons.add_subparsers(dest="action", required=True)
    for action in ("status", "pause", "resume"):
        actions.add_parser(action)
    dry = actions.add_parser("run")
    dry.add_argument("--dry-run", action="store_true", required=True)
    dlq = sub.add_parser("dead-letter")
    dlq_actions = dlq.add_subparsers(dest="action", required=True)
    dlq_actions.add_parser("status")
    listing = dlq_actions.add_parser("list")
    listing.add_argument("event_id", nargs="?")
    ack = dlq_actions.add_parser("acknowledge")
    ack.add_argument("event_id")
    ack.add_argument("--resolution", default="historical_permanent")
    replay = dlq_actions.add_parser("replay")
    replay.add_argument("event_id")
    sub.add_parser("doctor").set_defaults(doctor=True)
    sub.add_parser("screen").set_defaults(screen=True)
    sub.add_parser("migrate").set_defaults(path=None)
    args = parser.parse_args(argv)
    if getattr(args, "doctor", False):
        print(json.dumps(_doctor(), ensure_ascii=False, indent=2, default=str))
        return 0
    if getattr(args, "screen", False):
        print(json.dumps(_status_screen(), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "migrate":
        from scripts.migrate import main as migrate

        return migrate()
    if args.command == "consolidation":
        action = "run --dry-run" if args.action == "run" else args.action
        print(json.dumps(_consolidation(action), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "dead-letter":
        action = {"status": "stats"}.get(args.action, args.action)
        print(json.dumps(_dlq(args.event_id if hasattr(args, "event_id") else None, action,
                              getattr(args, "resolution", "")), ensure_ascii=False, indent=2, default=str))
        return 0
    print(json.dumps(_get(args.path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
