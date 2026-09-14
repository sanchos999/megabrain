"""Isolated scheduler tests; fake repository and transport, never production DB."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from consolidation.worker import ConsolidationWorker, Settings


class FakePG:
    def __init__(self): self.items=[]; self.rollback=False
    class _Conn:
        def __init__(self, pg): self.pg=pg
        def transaction(self):
            import contextlib
            @contextlib.contextmanager
            def ctx():
                if self.pg.rollback:
                    raise RuntimeError("simulated_db_failure")
                yield
            return ctx()
        def commit(self): pass
        def rollback(self): self.pg.rolled_back=True
    @property
    def conn(self): return FakePG._Conn(self)
    def add_derived_item(self, project, kind, content, source_ids, **kwargs):
        key=(project,kind,content.get("title"),tuple(source_ids))
        if key in {(x[0],x[1],x[2],tuple(x[3])) for x in self.items}: return None
        self.items.append((project,kind,content.get("title"),source_ids,kwargs)); return {"item_id":"fake"}

class FakeRepo:
    def __init__(self, projects): self.pg=FakePG(); self.projects=projects; self.runs=[]; self.calls=0
    def eligible_project(self):
        now=datetime.now(UTC); ready=[]
        for project,data in self.projects.items():
            if data["paused"] or data["next"]>now: continue
            if len(data["events"])>=20 or (now-data["first"]).total_seconds()>=1800: ready.append((data["first"],project))
        return min(ready)[1] if ready else None
    def eligible_projects(self):
        return [project for project, data in self.projects.items() if not data["paused"] and data["next"] <= datetime.now(UTC) and (len(data["events"]) >= 20 or (datetime.now(UTC)-data["first"]).total_seconds() >= 1800)]

    def events(self, project): return list(self.projects[project]["events"])
    def explicit_source_ids(self, project): return set()
    def daily_input_tokens_used(self): return 0

    def budget(self): return 0.0,0.0
    def duplicate(self, project, digest): return any(r[0]==project and r[1]==digest for r in self.runs)
    def reserve_dispatch(self, project, events, digest):
        if sum(r[2] in {"dispatching","success","failed"} for r in self.runs)>=2: return False,"RATE_LIMIT_HOURLY"
        self.runs.append((project,digest,"dispatching",{})); self.calls+=1; return True,None
    def record(self, project, events, digest, status, **values):
        for i,r in enumerate(self.runs):
            if r[0]==project and r[1]==digest: self.runs[i]=(project,digest,status,values); return
        self.runs.append((project,digest,status,values))
    def success(self, project, events, *, commit=True):
        self.projects[project]["events"]=self.projects[project]["events"][len(events):]
        self.projects[project]["first"]=datetime.now(UTC); self.projects[project]["next"]=datetime.now(UTC)+timedelta(seconds=900)
    def failure(self, project, error, retry): self.projects[project]["next"]=datetime.now(UTC)+timedelta(seconds=60)

def event(project,number,text="A meaningful project event with enough content"):
    return {"event_id":f"evt_{project}_{number}","project_id":project,"event_type":"USER_MESSAGE","created_at":"2026-09-14T10:00:00Z","payload":{"text":text},"metadata":{}}
def repo(*projects,age_minutes=0,count=1):
    first=datetime.now(UTC)-timedelta(minutes=age_minutes)
    return FakeRepo({p:{"events":[event(p,i) for i in range(count)],"first":first,"next":datetime.now(UTC),"paused":False} for p in projects})

class FakeTransport:
    def __init__(self,fail=None): self.requests=[]; self.fail=fail
    def __call__(self,body):
        import re
        self.requests.append(body)
        if self.fail: raise self.fail
        source=re.search(r"\[(evt_[^\]]+)\]",body["messages"][0]["content"]).group(1)
        content='{"items":[{"kind":"EXPERIENCE","title":"lesson","confidence":0.8,"source_event_ids":["'+source+'"]}]}'
        return ({"choices":[{"message":{"content":content}}]},{"x-gateway-selected-slug":"cheap-summary","x-gateway-selected-provider":"gateway-provider"},{"prompt_tokens":100,"completion_tokens":20})

def test_empty_and_below_threshold_do_not_call():
    r=repo("A"); w=ConsolidationWorker(r,FakeTransport()); assert w.run_once()["status"]=="idle"; assert not w.transport.requests
def test_max_wait_releases_one_batch():
    r=repo("A",age_minutes=31,count=5); t=FakeTransport(); x=ConsolidationWorker(r,t).run_once(); assert x["llm_calls"]==1 and x["events"]==5 and len(t.requests)==1
def test_threshold_releases_batch_not_each_event():
    r=repo("A",count=20); t=FakeTransport(); assert ConsolidationWorker(r,t).run_once()["events"]==20 and len(t.requests)==1
def test_max_batch_is_bounded():
    r=repo("A",count=100); t=FakeTransport(); assert ConsolidationWorker(r,t,Settings(max_batch_events=25)).run_once()["events"]==25
def test_oldest_project_fixture_scope_ignores_production():
    r=repo("production-backlog","fixture-A","fixture-B",age_minutes=31,count=20); r.projects["production-backlog"]["first"]=datetime.now(UTC)-timedelta(minutes=1); t=FakeTransport(); ConsolidationWorker(r,t).run_once(); assert "production-backlog" not in t.requests[0]["messages"][0]["content"] and r.projects["production-backlog"]["events"]
def test_failure_keeps_watermark_and_backoff():
    r=repo("A",age_minutes=31,count=20); before=list(r.projects["A"]["events"]); assert ConsolidationWorker(r,FakeTransport(RuntimeError("502"))).run_once()["status"]=="failed"; assert r.projects["A"]["events"]==before
def test_429_not_retried_same_tick():
    r=repo("A",age_minutes=31,count=20); t=FakeTransport(RuntimeError("429")); w=ConsolidationWorker(r,t); assert w.run_once()["status"]=="failed" and w.run_once()["status"]=="idle" and len(t.requests)==1
def test_idempotency_and_watermark():
    r=repo("A",age_minutes=31,count=20); t=FakeTransport(); w=ConsolidationWorker(r,t); assert w.run_once()["llm_calls"]==1; r.projects["A"]["events"]=[event("A",i) for i in range(20)]; r.projects["A"]["first"]=datetime.now(UTC)-timedelta(minutes=31); r.projects["A"]["next"]=datetime.now(UTC); assert w.run_once()["status"]=="duplicate" and len(t.requests)==1
def test_model_semantics_no_glm_pin():
    r=repo("A",age_minutes=31,count=20); t=FakeTransport(); ConsolidationWorker(r,t).run_once(); q=t.requests[0]; assert q["model"]=="main-auto" and q["extra_body"]["task_class"]=="SUMMARIZE" and q["extra_body"]["profile"]=="CHEAPEST" and "glm-5.3" not in str(q).lower()
def test_budget_stops_before_transport(monkeypatch):
    monkeypatch.setenv("MB_CONSOLIDATION_ESTIMATED_COST_PER_1K","100"); r=repo("A",age_minutes=31,count=20); t=FakeTransport(); assert ConsolidationWorker(r,t,Settings(max_cost_call=.01)).run_once()["status"]=="budget_paused" and not t.requests
def test_dry_run_never_calls_transport():
    r=repo("A",age_minutes=31,count=20); t=FakeTransport(); x=ConsolidationWorker(r,t).dry_run(); assert x["llm_calls"]==0 and not t.requests
def test_rate_guard_blocks_without_transport():
    r=repo("A",age_minutes=31,count=20); r.runs=[("old","x","success",{}),("old2","y","failed",{})]; t=FakeTransport(); x=ConsolidationWorker(r,t).run_once(); assert x["reason"]=="RATE_LIMIT_HOURLY" and not t.requests and len(r.projects["A"]["events"])==20
def test_failed_dispatch_consumes_rate_reservation():
    r=repo("A",age_minutes=31,count=20); t=FakeTransport(RuntimeError("502")); assert ConsolidationWorker(r,t).run_once()["status"]=="failed" and r.calls==1
