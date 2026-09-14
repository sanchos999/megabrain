"""Integration tests against an isolated PostgreSQL (never production).

Enabled only when MEGABRAIN_IT_DATABASE_URL is set (acceptance runs create the
database first). Covers: fresh migrations, upgrade path 0.1.1->0.1.2, atomic
consolidation transaction with artificial DB failure, watermark/idempotency
across worker restarts, durable heartbeats and stale-worker detection.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MEGABRAIN_IT_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="isolated integration DB not provided")


@pytest.fixture()
def db():
    from scripts.migrate import apply_migrations

    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()
    apply_migrations(DSN)
    yield DSN
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()


def _write_event(cur, project, n, text="Meaningful integration event about project work"):
    event_id = f"it_{project}_{n}_{uuid.uuid4().hex[:6]}"
    cur.execute("INSERT INTO projects (project_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (project, project))
    cur.execute("""INSERT INTO events (event_id,source,project_id,session_id,event_type,created_at,payload)
                   VALUES (%s,'test',%s,'s1','USER_MESSAGE',now(),%s)""",
                (event_id, project, __import__("json").dumps({"text": text})))
    cur.execute("""INSERT INTO consolidation_projects (project_id,first_dirty_at,last_dirty_at,
                   pending_event_count,next_eligible_at)
                   VALUES (%s, now()-interval '4 hours', now()-interval '3 hours', 1, now())
                   ON CONFLICT (project_id) DO UPDATE SET pending_event_count=
                   consolidation_projects.pending_event_count+1, last_dirty_at=now(), next_eligible_at=now()""",
                (project,))
    return event_id


def _worker(dsn, transport):
    from consolidation.worker import ConsolidationWorker, PostgresSchedulerRepository, Settings
    from storage.pg import Postgres

    pg = Postgres(dsn, None, 4096)
    settings = Settings(idle_s_important=60, idle_s_large=60, idle_s_medium=60, idle_s_small=60,
                        abs_max_wait_s=120)
    repo = PostgresSchedulerRepository(pg, settings)
    return ConsolidationWorker(repo, transport, settings), pg


def _ok_transport(body):
    import json as j
    import re

    source = re.search(r"\[(it_[^\]]+)\]", body["messages"][0]["content"]).group(1)
    content = j.dumps({"items": [{"kind": "EXPERIENCE", "title": "lesson", "confidence": 0.9,
                                  "importance": "HIGH", "source_event_ids": [source]}]})
    return ({"choices": [{"message": {"content": content}}]},
            {"x-gateway-selected-slug": "it-model", "x-gateway-selected-provider": "it"},
            {"prompt_tokens": 50, "completion_tokens": 10})


def test_fresh_migrations_latest(db):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM schema_migrations")
        assert cur.fetchone()[0] == 10


def test_atomicity_db_failure_no_partial_state(db):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        _write_event(cur, "atomic", 0)
    worker, pg = _worker(DSN, _ok_transport)
    original = worker.repository.record

    def failing_record(*args, **kwargs):
        original(*args, **{**kwargs, "commit": False})
        raise RuntimeError("injected_post_success_failure")

    worker.repository.record = failing_record
    result = worker.run_once()
    assert result["status"] == "failed"
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_items WHERE project_id='atomic' AND extractor_type='LLM'")
        assert cur.fetchone()[0] == 0, "derived items must roll back"
        cur.execute("SELECT pending_event_count,last_consolidated_event_id FROM consolidation_projects WHERE project_id='atomic'")
        pending, watermark = cur.fetchone()
        assert pending >= 1 and watermark is None, "watermark must not advance"
    pg.conn.close()


def test_watermark_and_duplicate_replay_after_restart(db):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        for i in range(3):
            _write_event(cur, "wm", i)
    worker, pg = _worker(DSN, _ok_transport)
    first = worker.run_once()
    assert first["status"] == "success" and first["llm_calls"] >= 1
    pg.conn.close()
    # "restart": fresh worker, same DB
    worker2, pg2 = _worker(DSN, _ok_transport)
    second = worker2.run_once()
    assert second["status"] in {"idle", "duplicate"} and second.get("llm_calls", 0) == 0
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT tokens_in,tokens_in_status,cost_status FROM consolidation_runs WHERE status='success'")
        tokens_in, tokens_status, cost_status = cur.fetchone()
        assert tokens_in == 50 and tokens_status == "REPORTED" and cost_status == "UNKNOWN"
    pg2.conn.close()


def test_unknown_usage_estimated_not_zero(db):
    def no_usage(body):
        resp, headers, _ = _ok_transport(body)
        return resp, headers, {}
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        _write_event(cur, "unk", 0)
    worker, pg = _worker(DSN, no_usage)
    assert worker.run_once()["status"] == "success"
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT tokens_in,tokens_in_status,tokens_in_estimated,tokens_out_status
                       FROM consolidation_runs WHERE status='success'""")
        tokens_in, tokens_status, est, out_status = cur.fetchone()
        assert tokens_in is None and tokens_status == "ESTIMATED" and est > 0 and out_status == "UNKNOWN"
    pg.conn.close()


def test_heartbeat_and_stale_detection(db):
    from operations import WorkerHeartbeat, worker_rows
    from storage.pg import Postgres

    beat = WorkerHeartbeat("testcomp", dsn=DSN)
    assert beat.update(state="RUNNING", success=True, processed_items=5)
    pg = Postgres(DSN, None, 4096)
    rows = worker_rows(pg)
    assert rows["testcomp"]["state"] == "RUNNING" and rows["testcomp"]["health"] == "OK"
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE worker_status SET last_heartbeat_at=now()-interval '30 minutes' WHERE component='testcomp'")
    rows = worker_rows(pg)
    assert rows["testcomp"]["health"] == "ERROR", "stale heartbeat must be visible"
    pg.conn.close()


def test_daily_token_guard_durable(db):
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        _write_event(cur, "tok", 0)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO consolidation_runs (run_id,project_id,batch_id,event_count,input_hash,
                       model_requested,status,tokens_in,tokens_in_status,started_at)
                       VALUES ('seed','tok','b',1,'h','main-auto','success',99900,'REPORTED',now())""")
    worker, pg = _worker(DSN, _ok_transport)
    worker.settings = type(worker.settings)(max_input_tokens_per_day=100_000)
    result = worker.run_once()
    assert result["status"] == "token_budget_daily" and result["llm_calls"] == 0
    pg.conn.close()
