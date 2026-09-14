"""Public MegaBrain CLI."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request


def _base() -> str:
    return os.environ.get("MEGABRAIN_API_URL", f"http://{os.environ.get('MEGABRAIN_HOST', '127.0.0.1')}:{os.environ.get('MEGABRAIN_PORT', '4300')}")


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
            cur.execute("SELECT COALESCE(sum(estimated_cost),0) FROM consolidation_runs WHERE started_at >= date_trunc('month',now()) AND status IN ('dispatching','success','failed')")
            cost_month = float(cur.fetchone()[0])
            cur.execute("""SELECT finished_at,model_selected,provider,event_count,tokens_in,tokens_out,estimated_cost,status,error_class
                           FROM consolidation_runs ORDER BY finished_at DESC NULLS LAST LIMIT 1""")
            last = cur.fetchone()
    except Exception as error:
        return {"status": "unavailable", "worker": _worker_state(), "error": type(error).__name__, "detail": str(error)[:200]}
    return {"status": "ok", "worker": _worker_state(),
            "dirty_projects": queue, "pending_events": int(pending),
            "oldest_pending": oldest.isoformat() if oldest else None,
            "next_eligible_run": next_eligible.isoformat() if next_eligible else None,
            "current_backoff_retry": int(retries or 0), "calls_last_hour": int(calls_hour), "hourly_limit": 2, "hourly_remaining": max(0, 2-int(calls_hour)), "calls_today": int(calls_day), "daily_limit": 24, "daily_remaining": max(0, 24-int(calls_day)), "cost_today": float(cost_day), "daily_cost_limit": 25.0, "cost_month": cost_month, "monthly_cost_limit": 250.0, "budget_state": "BUDGET_PAUSED" if budget_paused else "READY",
            "last_consolidation": dict(zip(("finished_at", "model", "provider", "batch_size", "tokens_in", "tokens_out", "estimated_cost", "status", "error_class"), last)) if last else None}


def _worker_state() -> str:
    return subprocess.run(["systemctl", "--user", "is-active", "megabrain-consolidation-worker.service"], capture_output=True, text=True).stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="megabrain")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, path in (("health", "/health"), ("status", "/version"), ("doctor", "/health"), ("projects", "/v1/projects")):
        sub.add_parser(name).set_defaults(path=path)
    cons = sub.add_parser("consolidation")
    actions = cons.add_subparsers(dest="action", required=True)
    for action in ("status", "pause", "resume"):
        actions.add_parser(action)
    dry = actions.add_parser("run")
    dry.add_argument("--dry-run", action="store_true", required=True)
    sub.add_parser("migrate").set_defaults(path=None)
    args = parser.parse_args(argv)
    if args.command == "migrate":
        from scripts.migrate import main as migrate
        return migrate()
    if args.command == "consolidation":
        action = "run --dry-run" if args.action == "run" else args.action
        print(json.dumps(_consolidation(action), ensure_ascii=False, indent=2, default=str))
        return 0
    print(json.dumps(_get(args.path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
