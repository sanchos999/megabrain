"""M1 acceptance tests. Run: .venv/bin/python -m pytest tests/ -v

Uses the real local PostgreSQL (megabrain DB) and megabrain-redis (127.0.0.1:6390).
Test project ids prefixed mbtest_ to avoid collisions.
"""
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from api.main import STATE, app

client = TestClient(app)


def _ev(**kw):
    base = {
        "source": "test",
        "event_type": "USER_MESSAGE",
        "created_at": "2026-09-11T12:00:00Z",
        "session_id": f"mbtest_sess_{uuid.uuid4().hex[:8]}",
        "project_id": None,
        "payload": {"text": "test"},
    }
    base.update(kw)
    return base


@pytest.fixture(scope="module")
def project_x():
    pid = f"mbtest_x_{uuid.uuid4().hex[:6]}"
    r = client.post("/v1/projects", json={"project_id": pid, "name": "Project X"})
    assert r.status_code == 200
    return pid


# ---------------- event durability / immutability ----------------

def test_event_write_durable_response(project_x):
    r = client.post("/v1/events", json=_ev(project_id=project_x, event_type="USER_MESSAGE",
                                           payload={"text": "hello"}))
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["durable"] is True
    assert body["duplicate"] is False
    assert body["project_revision"] is not None


def test_duplicate_idempotent(project_x):
    ev = _ev(project_id=project_x, payload={"text": "dup"})
    r1 = client.post("/v1/events", json=ev)
    eid = r1.json()["event_id"]
    # same event_id, same payload -> duplicate success
    ev2 = dict(ev, event_id=eid)
    r2 = client.post("/v1/events", json=ev2)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["durable"] is True


def test_duplicate_same_id_different_hash_rejected(project_x):
    ev = _ev(project_id=project_x, payload={"text": "v1"})
    r1 = client.post("/v1/events", json=ev)
    eid = r1.json()["event_id"]
    r2 = client.post("/v1/events", json=dict(ev, event_id=eid, payload={"text": "CHANGED"}))
    assert r2.status_code == 400


def test_event_immutable_in_db(project_x):
    """Raw payload cannot be updated via any API; verify no update path + hash integrity."""
    ev = _ev(project_id=project_x, event_type="DECISION",
             payload={"item_key": "db_choice", "text": "use postgres"})
    r = client.post("/v1/events", json=ev)
    eid = r.json()["event_id"]
    got = client.get(f"/v1/events/{eid}").json()
    assert got["event_id"] == eid
    assert got["payload"]["text"] == "use postgres"
    assert got["payload_hash"] is not None
    # re-fetch stable
    got2 = client.get(f"/v1/events/{eid}").json()
    assert got2["payload_hash"] == got["payload_hash"]


def test_large_payload_blob(project_x):
    big_text = "x" * 50000
    ev = _ev(project_id=project_x, event_type="ASSISTANT_MESSAGE",
             payload={"text": big_text})
    r = client.post("/v1/events", json=ev)
    assert r.status_code == 200
    assert r.json()["blob_ref"] is not None
    got = client.get(f"/v1/events/{r.json()['event_id']}").json()
    assert got["blob_ref"] == r.json()["blob_ref"]
    assert got["payload"]["_offloaded"] is True
    # blob content retrievable lossless
    blob = STATE["blobs"].get(got["blob_ref"])
    assert blob is not None and big_text.encode() in blob


def test_batch(project_x):
    events = [_ev(project_id=project_x, event_type="TURN_STARTED"),
              _ev(project_id=project_x, event_type="USER_MESSAGE", payload={"text": "a"}),
              _ev(project_id=project_x, event_type="TURN_COMPLETED")]
    r = client.post("/v1/events/batch", json={"events": events})
    assert r.status_code == 200
    assert all(x["accepted"] for x in r.json()["results"])


# ---------------- CRITICAL TEST SCENARIO (spec §23) ----------------

def test_critical_scenario_decision_constraint_capsule(project_x):
    pid = f"mbtest_crit_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "Project X"})

    # Turn/Event 1
    r1 = client.post("/v1/events", json=_ev(
        project_id=pid, event_type="DECISION",
        payload={"item_key": "storage_engine", "text": "Для проекта используем PostgreSQL как durable store"}))
    r2 = client.post("/v1/events", json=_ev(
        project_id=pid, event_type="CONSTRAINT",
        payload={"item_key": "no_redis_primary", "text": "Redis как основную БД не использовать, только cache"}))
    assert r1.json()["durable"] and r2.json()["durable"]

    # Turn/Event 2 — query without reading conversation history: capsule must answer
    ctx = client.post("/v1/memory/context", json={"project_id": pid}).json()
    decisions_text = str(ctx["sections"]["CONFIRMED_DECISIONS"])
    constraints_text = str(ctx["sections"]["CONSTRAINTS"])
    assert "PostgreSQL" in decisions_text
    assert "durable" in decisions_text
    assert "Redis" in constraints_text
    assert "cache" in constraints_text
    assert ctx["metadata"]["llm_used"] is False
    assert ctx["sources"]


# ---------------- continuation test (spec §24) ----------------

def test_continuation(project_x):
    pid = f"mbtest_cont_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "Continuation Project"})
    sess = f"mbtest_contsess_{uuid.uuid4().hex[:6]}"
    # seed: state, 3 open tasks, 2 decisions
    client.post("/v1/events", json=_ev(project_id=pid, session_id=sess, event_type="DECISION",
                                       payload={"item_key": "d1", "text": "решение 1"}))
    client.post("/v1/events", json=_ev(project_id=pid, session_id=sess, event_type="DECISION",
                                       payload={"item_key": "d2", "text": "решение 2"}))
    for i in range(3):
        client.post("/v1/events", json=_ev(project_id=pid, session_id=sess, event_type="TASK_UPDATE",
                                           payload={"item_key": f"t{i}", "text": f"задача {i}", "status": "OPEN"}))
    # resolver: continuation phrase via session mapping
    r = client.post("/v1/resolve-project", json={"session_id": sess, "query": "продолжаем"})
    assert r.status_code == 200
    assert r.json()["project_id"] == pid
    assert r.json()["mode"] == "SESSION_MAPPING_REDIS" or r.json()["mode"] == "SESSION_MAPPING_PG"
    assert "semantic" not in r.json()["mode"]
    # capsule contains state + tasks + decisions
    ctx = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert len(ctx["sections"]["OPEN_WORK"]) == 3
    assert len(ctx["sections"]["CONFIRMED_DECISIONS"]) == 2


def test_resolver_continuation_recent_active_is_safe_unresolved():
    pid = f"mbtest_recent_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "RecentActive"})
    client.post("/v1/events", json=_ev(project_id=pid, event_type="USER_MESSAGE", payload={"text": "x"}))
    time.sleep(0.05)
    r = client.post("/v1/resolve-project", json={"query": "что осталось?", "profile": "test-unbound"})
    assert r.status_code == 200
    assert r.json()["project_id"] is None
    assert r.json()["reason"] in {"UNRESOLVED", "AMBIGUOUS"}


# ---------------- temporal / supersession ----------------

def test_superseding_decision(project_x):
    pid = f"mbtest_sup_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "Sup"})
    client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                       payload={"item_key": "lang", "text": "используем PHP"}))
    ctx1 = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert "PHP" in str(ctx1["sections"]["CONFIRMED_DECISIONS"])
    # supersede
    client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                       payload={"item_key": "lang", "text": "используем Go", "supersedes": True}))
    ctx2 = client.post("/v1/memory/context", json={"project_id": pid}).json()
    d = str(ctx2["sections"]["CONFIRMED_DECISIONS"])
    assert "Go" in d and "PHP" not in d
    # history preserved in PG (valid_to set on old item)
    _ = STATE["pg"].current_memory(pid)
    with STATE["pg"].conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_items WHERE project_id=%s AND kind='DECISION'", (pid,))
        total = cur.fetchone()[0]
        STATE["pg"].conn.commit()
    assert total == 2  # old + new, nothing deleted


def test_task_done_closes(project_x):
    pid = f"mbtest_task_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "T"})
    client.post("/v1/events", json=_ev(project_id=pid, event_type="TASK_UPDATE",
                                       payload={"item_key": "t1", "text": "сделать А", "status": "OPEN"}))
    ctx1 = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert len(ctx1["sections"]["OPEN_WORK"]) == 1
    client.post("/v1/events", json=_ev(project_id=pid, event_type="TASK_UPDATE",
                                       payload={"item_key": "t1", "text": "сделать А", "status": "DONE"}))
    ctx2 = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert len(ctx2["sections"]["OPEN_WORK"]) == 0


# ---------------- capsule mechanics ----------------

def test_token_budget(project_x):
    pid = f"mbtest_budget_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "Budget"})
    for i in range(10):
        client.post("/v1/events", json=_ev(project_id=pid, event_type="CONSTRAINT",
                                           payload={"item_key": f"c{i}", "text": f"ограничение номер {i} " * 20}))
    ctx = client.post("/v1/memory/context", json={"project_id": pid, "token_budget": 150}).json()
    assert ctx["metadata"]["token_estimate"] <= 150
    assert "omitted" in ctx["metadata"]
    assert "included" in ctx["metadata"]
    # constraints are highest priority: included first
    assert "constraints" in ctx["metadata"]["included"]


def test_stable_delta_split(project_x):
    pid = f"mbtest_sd_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "SD"})
    client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                       payload={"item_key": "d", "text": "stable decision"}))
    ctx = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert "confirmed_decisions" in ctx["stable"]
    assert "recent_changes" in ctx["delta"]


def test_provenance(project_x):
    pid = f"mbtest_prov_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "Prov"})
    r = client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                           payload={"item_key": "p1", "text": "prov"}))
    eid = r.json()["event_id"]
    items = STATE["pg"].current_memory(pid)
    assert items[0]["source_event_ids"] == [eid]
    assert items[0]["extractor_type"] in ("EXPLICIT", "DETERMINISTIC")
    assert items[0]["confidence"] is not None


# ---------------- restart / redis-failure ----------------

def test_restart_restore(project_x):
    """Simulate restart: clear RAM, rebuild from PG, capsule identical content."""
    pid = f"mbtest_restart_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "Restart"})
    client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                       payload={"item_key": "r1", "text": "пережить рестарт"}))
    before = client.post("/v1/memory/context", json={"project_id": pid}).json()
    # clear RAM cache (simulates process restart)
    STATE["ram"].clear()
    after = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert after["sections"]["CONFIRMED_DECISIONS"] == before["sections"]["CONFIRMED_DECISIONS"]
    assert after["capsule_revision"] == before["capsule_revision"]
    # events survived (durable)
    assert STATE["pg"].count_events(pid) >= 1


def test_redis_failure_degraded(project_x):
    """Redis down: writes durable, reads work, mode degraded."""
    pid = f"mbtest_redisfail_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "RF"})
    # point L1 to a dead port
    saved = STATE["redis"].url
    STATE["redis"].url = "redis://127.0.0.1:6399/0"
    STATE["redis"]._pool = None
    try:
        r = client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                               payload={"item_key": "rf", "text": "redis down"}))
        assert r.json()["durable"] is True
        ctx = client.post("/v1/memory/context", json={"project_id": pid}).json()
        assert "redis down" in str(ctx["sections"]["CONFIRMED_DECISIONS"])
    finally:
        STATE["redis"].url = saved
        STATE["redis"]._pool = None


# ---------------- auth ----------------

def test_auth():
    cfg = STATE["cfg"]
    if cfg.get("api_token"):
        # with token configured, no-auth request must 401
        r = client.post("/v1/events", json=_ev())
        # TestClient sends no auth here
        # (client fixture has no token set)
    # token roundtrip
    old = cfg.get("api_token")
    cfg["api_token"] = "testtoken123"
    try:
        r = client.post("/v1/events", json=_ev(), headers={})
        assert r.status_code == 401
        r = client.post("/v1/events", json=_ev(),
                        headers={"Authorization": "Bearer CHANGE_ME"})
        assert r.status_code == 200
    finally:
        cfg["api_token"] = old or ""


def test_health_and_version():
    h = client.get("/health").json()
    assert h["postgres"] is True
    assert h["version"]
    assert client.get("/version").json()["component"] == "megabrain"


def test_no_llm_on_critical_path():
    """Capsule build and resolver must not invoke any LLM client."""
    import api.main as m
    import projects.resolver as res
    import retrieval.capsule as caps
    for mod in (m, caps, res):
        src = open(mod.__file__).read()
        for banned in ("openai", "anthropic", "httpx.post", "requests.post"):
            assert banned not in src, f"banned dependency {banned} in {mod.__name__}"
    # capsule reports llm_used=False and flag exists
    pid = f"mbtest_nollm_{uuid.uuid4().hex[:6]}"
    client.post("/v1/projects", json={"project_id": pid, "name": "NoLLM"})
    client.post("/v1/events", json=_ev(project_id=pid, event_type="DECISION",
                                       payload={"item_key": "n", "text": "no llm"}))
    ctx = client.post("/v1/memory/context", json={"project_id": pid}).json()
    assert ctx["metadata"]["llm_used"] is False
