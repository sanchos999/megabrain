"""MegaBrain 0.1.2 hardening regression tests (fake repo/transport; REAL LLM = 0)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from consolidation.worker import ConsolidationWorker, Settings, eligibility, importance_signal, prefilter


class FakePG:
    def __init__(self): self.items = []; self.rollback = False; self.rolled_back = False
    class _Conn:
        def __init__(self, pg): self.pg = pg
        def transaction(self):
            import contextlib
            @contextlib.contextmanager
            def ctx():
                if self.pg.rollback:
                    raise RuntimeError("simulated_db_failure")
                yield
            return ctx()
        def commit(self): pass
        def rollback(self): self.pg.rolled_back = True
    @property
    def conn(self): return FakePG._Conn(self)
    def add_derived_item(self, project, kind, content, source_ids, **kwargs):
        self.items.append((project, kind, content, source_ids, kwargs)); return {"item_id": "fake"}


class FakeRepo:
    """Mirrors the scheduler contract; tracks recorded run values."""
    def __init__(self, projects):
        self.pg = FakePG(); self.projects = projects; self.runs = []; self.calls = 0
        self.explicit = set(); self.tokens_used = 0
    def eligible_project(self):
        now = datetime.now(UTC); ready = []
        for project, data in self.projects.items():
            if data["paused"] or data["next"] > now: continue
            important = any(importance_signal(e["payload"].get("text", "")) >= 2 for e in data["events"])
            if eligibility(len(data["events"]), (now - data["first"]).total_seconds(),
                           (now - data["first"]).total_seconds(), important, Settings()):
                ready.append((data["first"], project))
        return min(ready)[1] if ready else None
    def eligible_projects(self): return [self.eligible_project()] if self.eligible_project() else []
    def defer_project(self, project, reason): self.projects[project]["paused"] = reason
    def events(self, project): return list(self.projects[project]["events"])
    def explicit_source_ids(self, project): return set(self.explicit)
    def daily_input_tokens_used(self): return self.tokens_used
    def budget(self): return 0.0, 0.0
    def duplicate(self, project, digest): return any(r[0] == project and r[1] == digest for r in self.runs)
    def reserve_dispatch(self, project, events, digest):
        if sum(r[2] in {"dispatching", "success", "failed"} for r in self.runs) >= 2:
            return False, "RATE_LIMIT_HOURLY"
        self.runs.append((project, digest, "dispatching", {})); self.calls += 1; return True, None
    def record(self, project, events, digest, status, *, commit=True, **values):
        for i, r in enumerate(self.runs):
            if r[0] == project and r[1] == digest: self.runs[i] = (project, digest, status, values); return
        self.runs.append((project, digest, status, values))
    def success(self, project, events, *, commit=True):
        self.projects[project]["events"] = self.projects[project]["events"][len(events):]
        self.projects[project]["first"] = datetime.now(UTC)
        self.projects[project]["next"] = datetime.now(UTC) + timedelta(seconds=900)
    def failure(self, project, error, retry):
        self.projects[project]["next"] = datetime.now(UTC) + timedelta(seconds=60)


def event(project, number, text="A meaningful project event with enough content", kind="USER_MESSAGE"):
    return {"event_id": f"evt_{project}_{number}", "project_id": project, "event_type": kind,
            "created_at": "2026-09-14T10:00:00Z", "payload": {"text": text}, "metadata": {}}


def repo(*projects, age_minutes=150, count=1):
    first = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return FakeRepo({p: {"events": [event(p, i) for i in range(count)], "first": first,
                         "next": datetime.now(UTC), "paused": False} for p in projects})


def items_body(source, confidence=0.8, kind="EXPERIENCE", importance="NORMAL"):
    return json.dumps({"items": [{"kind": kind, "title": "lesson", "confidence": confidence,
                                  "importance": importance, "source_event_ids": [source]}]})


class StageTransport:
    """Configurable transport: per-call content/confidence and profile capture."""
    def __init__(self, contents=None, fail=None): self.requests = []; self.contents = contents or []; self.fail = fail
    def __call__(self, body):
        self.requests.append(body)
        if self.fail: raise self.fail
        import re
        source = re.search(r"\[(evt_[^\]]+)\]", body["messages"][0]["content"]).group(1)
        content = self.contents.pop(0) if self.contents else items_body(source)
        return ({"choices": [{"message": {"content": content}}]},
                {"x-gateway-selected-slug": "cheap-summary", "x-gateway-selected-provider": "gateway-provider"},
                {"prompt_tokens": 100, "completion_tokens": 20})


# ---- root cause regression: router 502 -> failed run, watermark kept, batch repeatable
def test_router_502_runtime_error_regression():
    r = repo("A", age_minutes=150, count=5)
    result = ConsolidationWorker(r, StageTransport(fail=RuntimeError("router_retryable_http_502"))).run_once()
    assert result["status"] == "failed" and result["llm_calls"] == 1 and result["error"] == "RuntimeError"
    assert r.runs[0][2] == "failed" and r.runs[0][3]["error_class"] == "RuntimeError"
    assert r.projects["A"]["events"], "watermark must not advance on failure"


# ---- atomicity: DB failure inside the success transaction leaves no partial state
def test_transaction_rollback_no_partial_derived():
    r = repo("A", age_minutes=150, count=5)
    r.pg.rollback = True  # simulate DB failure at commit stage
    result = ConsolidationWorker(r, StageTransport()).run_once()
    assert result["status"] == "failed"
    assert not r.pg.items, "derived memory must not partially persist"
    assert r.projects["A"]["events"], "watermark must not advance"
    assert r.pg.rolled_back


# ---- tokens/cost honesty
def test_unknown_tokens_never_recorded_as_zero():
    r = repo("A", age_minutes=150, count=5)
    class NoUsage(StageTransport):
        def __call__(self, body):
            resp, headers, _ = super().__call__(body)
            return resp, headers, {}  # Router gave no usage
    ConsolidationWorker(r, NoUsage()).run_once()
    values = r.runs[0][3]
    assert values["tokens_in"] is None and values["tokens_in_status"] == "ESTIMATED"
    assert values["tokens_in_estimated"] > 0
    assert values["tokens_out"] is None and values["tokens_out_status"] == "UNKNOWN"
    assert values["estimated_cost"] is None and values["cost_status"] == "UNKNOWN"


def test_reported_tokens_recorded_as_reported():
    r = repo("A", age_minutes=150, count=5)
    ConsolidationWorker(r, StageTransport()).run_once()
    values = r.runs[0][3]
    assert values["tokens_in"] == 100 and values["tokens_in_status"] == "REPORTED"
    assert values["tokens_out"] == 20 and values["tokens_out_status"] == "REPORTED"


# ---- adaptive batching windows
# ---- adaptive batching windows (§21)
def test_adaptive_eligibility_windows():
    s = Settings(idle_s_important=900, idle_s_large=1800, idle_s_medium=7200,
                 idle_s_small=43200, abs_max_wait_s=86400, large_n=20, medium_n=5)
    # 1-4 normal events wait the small window (6-12h)
    assert not eligibility(3, age_dirty_s=3600, age_first_s=3600, important=False, s=s)
    # importance fast path: eligible after 15 min idle
    assert eligibility(3, age_dirty_s=1000, age_first_s=1000, important=True, s=s)
    # 20+ events: 30 min
    assert not eligibility(25, age_dirty_s=1700, age_first_s=1700, important=False, s=s)
    assert eligibility(25, age_dirty_s=1900, age_first_s=1900, important=False, s=s)
    # 5-19 events: 2 hours
    assert not eligibility(10, age_dirty_s=5000, age_first_s=5000, important=False, s=s)
    assert eligibility(10, age_dirty_s=7300, age_first_s=7300, important=False, s=s)
    # absolute 24h max wait overrides everything
    assert eligibility(1, age_dirty_s=60, age_first_s=86500, important=False, s=s)
    # nothing pending -> never eligible
    assert not eligibility(0, age_dirty_s=9e9, age_first_s=9e9, important=False, s=s)


def test_importance_fast_path_dispatches_early():
    important_text = ("Принято решение: мигрировать на новую архитектуру. "
                      "Ограничение: не трогать production без отката. Урок: regression после деплоя.")
    assert importance_signal(important_text) >= 2
    r = repo("A", age_minutes=16, count=3)
    r.projects["A"]["events"] = [event("A", i, important_text) for i in range(3)]
    # eligible_project returned the project (fast path decided by repo); worker dispatches without 12h wait
    result = ConsolidationWorker(r, StageTransport()).run_once()
    assert result.get("llm_calls") == 1


# ---- two-stage quality escalation
def test_escalation_on_low_confidence_uses_stronger_profile():
    r = repo("A", age_minutes=150, count=5)
    t = StageTransport(contents=[items_body("evt_A_0", confidence=0.2), items_body("evt_A_1", confidence=0.9)])
    result = ConsolidationWorker(r, t).run_once()
    assert len(t.requests) == 2, "low confidence must trigger a second, stronger call"
    assert t.requests[1]["extra_body"]["profile"] != t.requests[0]["extra_body"]["profile"]
    assert result["escalation_stage"] == 2 and result["escalation_reason"] == "LOW_CONFIDENCE"
    assert result["llm_calls"] == 2


def test_escalation_on_structural_validation_failure():
    r = repo("A", age_minutes=150, count=5)
    t = StageTransport(contents=["not json at all", items_body("evt_A_0")])
    result = ConsolidationWorker(r, t).run_once()
    assert len(t.requests) == 2 and result["escalation_reason"] == "STRUCTURAL_VALIDATION_FAILURE"


def test_no_escalation_when_confident():
    r = repo("A", age_minutes=150, count=5)
    t = StageTransport()
    result = ConsolidationWorker(r, t).run_once()
    assert len(t.requests) == 1 and result["escalation_stage"] == 1


def test_important_content_loses_cheap_items_escalates():
    important_text = ("Архитектурное решение: смена storage. Root cause найден в миграции. "
                      "Ограничение совместимости сохраняется.")
    r = repo("A", age_minutes=150, count=3)
    r.projects["A"]["events"] = [event("A", i, important_text) for i in range(3)]
    t = StageTransport(contents=['{"items": []}', items_body("evt_A_0")])
    result = ConsolidationWorker(r, t).run_once()
    assert len(t.requests) == 2 and result["escalation_reason"] == "IMPORTANT_CONTENT_LOSS"


# ---- importance validation on derived items
def test_invalid_importance_normalized_valid_set():
    r = repo("A", age_minutes=150, count=5)
    ConsolidationWorker(r, StageTransport(contents=[items_body("evt_A_0", importance="ULTRA")])).run_once()
    assert r.pg.items[0][2]["importance"] == "NORMAL"
    r2 = repo("A", age_minutes=150, count=5)
    ConsolidationWorker(r2, StageTransport(contents=[items_body("evt_A_0", importance="CRITICAL")])).run_once()
    assert r2.pg.items[0][2]["importance"] == "CRITICAL"


# ---- daily token guard
def test_daily_token_guard_blocks_and_reports_throttled():
    r = repo("A", age_minutes=150, count=5)
    r.tokens_used = 99_999
    settings = Settings(max_input_tokens_per_day=100_000)
    result = ConsolidationWorker(r, StageTransport(), settings).run_once()
    assert result["status"] == "token_budget_daily" and result["llm_calls"] == 0
    assert result["important_pending"] is False


# ---- explicit memory dedup
def test_explicit_memory_not_reconsolidated():
    r = repo("A", age_minutes=150, count=5)
    r.explicit = {"evt_A_0", "evt_A_1", "evt_A_2", "evt_A_3", "evt_A_4"}
    result = ConsolidationWorker(r, StageTransport()).run_once()
    assert result["status"] == "no_llm" and result["llm_calls"] == 0
    assert not r.projects["A"]["events"], "watermark advances past explicit-materialized events"


# ---- meaningful filter
def test_noise_events_excluded_from_batch_count():
    events = [event("A", 0), {"event_id": "evt_A_t1", "project_id": "A", "event_type": "TURN_STARTED",
                              "created_at": "z", "payload": {"text": "turn"}, "metadata": {}},
              {"event_id": "evt_A_t2", "project_id": "A", "event_type": "USER_MESSAGE",
               "created_at": "z", "payload": {"text": "ok"}, "metadata": {}}]
    assert [e["event_id"] for e in prefilter(events)] == ["evt_A_0"]


# ---- cross-project isolation: one batch = one project
def test_batch_never_mixes_projects():
    r = repo("A", "B", age_minutes=150, count=20)
    t = StageTransport()
    ConsolidationWorker(r, t).run_once()
    prompt = t.requests[0]["messages"][0]["content"]
    assert "evt_B_" not in prompt and "evt_A_" in prompt


# ---- circuit breaker
def test_circuit_breaker_backoff_then_half_open():
    r = repo("A", age_minutes=150, count=5)
    class BreakerBeat:
        def __init__(self): self.errors = 3
        def update(self, **kwargs): return True
        def error(self, error, detail=None): return True
    w = ConsolidationWorker(r, StageTransport(fail=RuntimeError("router_retryable_http_502")))
    w.heartbeat = BreakerBeat()
    w._breaker_state = lambda: ("BACKOFF", 3)
    assert w.run_once()["status"] == "breaker_backoff"
    w._breaker_state = lambda: ("HALF_OPEN", 3)
    result = w.run_once()
    assert result["status"] == "failed" and result.get("stage") == "HALF_OPEN"


# ---- watermark/idempotency: duplicate batch costs zero calls
def test_duplicate_batch_zero_calls():
    r = repo("A", age_minutes=150, count=5)
    t = StageTransport()
    w = ConsolidationWorker(r, t)
    w.run_once()
    r.projects["A"]["events"] = [event("A", i) for i in range(5)]
    r.projects["A"]["first"] = datetime.now(UTC) - timedelta(minutes=150)
    r.projects["A"]["next"] = datetime.now(UTC)
    result2 = w.run_once()
    assert result2["status"] == "duplicate" and result2["llm_calls"] == 0
    assert len(t.requests) == 1, "identical processed batch must cost zero new LLM calls"
