"""MegaBrain standalone REST API.

Failure semantics:
- PostgreSQL down: writes -> 503 durable=false. Reads -> 503.
- Redis down: writes still durable; reads served from PG/RAM; mode=DEGRADED.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core.config import load_config
from core.hot import L0RAM, L1Redis, Telemetry
from projects.resolver import ProjectResolver
from retrieval.capsule import CapsuleBuilder
from storage.pg import BlobStore, Postgres

VERSION = "0.1.0"

app = FastAPI(title="MegaBrain", version=VERSION)
bearer = HTTPBearer(auto_error=False)


def build_state() -> dict:
    cfg = load_config()
    telemetry = Telemetry()
    blobs = BlobStore(cfg["blob_dir"])
    pg = Postgres(cfg["postgres_dsn"], blobs, cfg["blob_inline_limit"])
    ram = L0RAM()
    redis_layer = L1Redis(cfg["redis_url"], cfg["redis_prefix"], telemetry)
    resolver = ProjectResolver(pg, ram, redis_layer, telemetry)
    capsules = CapsuleBuilder(pg, ram, redis_layer, telemetry, cfg)
    return {"cfg": cfg, "telemetry": telemetry, "blobs": blobs, "pg": pg,
            "ram": ram, "redis": redis_layer, "resolver": resolver, "capsules": capsules}


STATE = build_state()


async def require_auth(request: Request,
                       creds: HTTPAuthorizationCredentials | None = Security(bearer)):
    token = STATE["cfg"].get("api_token")
    if not token:
        return  # auth disabled (loopback dev default)
    if creds is None or creds.credentials != token:
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


# ---------- models ----------

class EventIn(BaseModel):
    event_id: str | None = None
    schema_version: int | None = 1
    source: str
    source_instance: str | None = None
    request_id: str | None = None
    turn_id: str | None = None
    sequence: int | None = None
    session_id: str | None = None
    project_id: str | None = None
    event_type: str
    created_at: str
    observed_at: str | None = None
    payload: Any | None = None
    payload_hash: str | None = None
    model: str | None = None
    route: str | None = None
    provider: str | None = None
    parent_event_id: str | None = None
    correlation_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class BatchIn(BaseModel):
    events: list[EventIn]


class ProjectIn(BaseModel):
    project_id: str
    name: str
    status: str = "ACTIVE"


class ResolveIn(BaseModel):
    project_id: str | None = None
    session_id: str | None = None
    query: str | None = None
    source: str = "hermes"
    profile: str = "default"
    channel: str = "cli"
    parent_session_id: str | None = None
    conversation_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None


class ContextIn(BaseModel):
    project_id: str | None = None
    session_id: str | None = None
    query: str | None = None
    token_budget: int | None = None
    since_revision: int | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None


# ---------- infra endpoints ----------

@app.get("/health")
async def health():
    st = STATE
    pg_ok = st["pg"].ping()
    redis_ok = st["redis"].ping()
    if pg_ok and redis_ok:
        mode = "OK"
    elif pg_ok:
        mode = "DEGRADED"
    else:
        mode = "DB_DOWN"
    return {"status": "ok" if pg_ok else "error", "postgres": pg_ok,
            "redis": redis_ok, "mode": mode, "version": VERSION}


@app.get("/version")
async def version():
    return {"version": VERSION, "component": "megabrain"}


@app.get("/metrics")
async def metrics(_=Depends(require_auth)):
    return STATE["telemetry"].snapshot()


# ---------- events ----------

def _write_one(ev: EventIn) -> dict:
    st = STATE
    t0 = time.perf_counter()
    try:
        result = st["pg"].append_event(ev.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        st["telemetry"].inc("event_write_failures")
        raise HTTPException(status_code=503, detail=f"durable write failed: {type(e).__name__}")
    dt = (time.perf_counter() - t0) * 1000
    st["telemetry"].observe("event_write_ms", dt)
    if result["duplicate"]:
        st["telemetry"].inc("event_duplicates")
    else:
        st["telemetry"].inc("event_writes")
    # non-blocking cache refresh (RAM + Redis); failure does not affect durability
    pid = ev.project_id
    if pid and not result["duplicate"]:
        st["ram"].invalidate(pid)
        # drop stale Redis hot state (no marker: absence = rebuild from PG)
        c = st["redis"]._client()
        if c is not None:
            try:
                c.delete(f"{st['cfg']['redis_prefix']}project:{pid}:hot")
            except Exception:
                pass
    return result


@app.post("/v1/events", dependencies=[Depends(require_auth)])
async def post_event(ev: EventIn):
    r = _write_one(ev)
    return {
        "event_id": r["event_id"],
        "accepted": True,
        "duplicate": r["duplicate"],
        "durable": True,  # PG commit happened (append_event commits before returning)
        "project_revision": r["project_revision"],
        "available_in_hot_memory": not r["duplicate"],
        "blob_ref": r.get("blob_ref"),
    }


@app.post("/v1/events/batch", dependencies=[Depends(require_auth)])
async def post_events_batch(batch: BatchIn):
    results = []
    for ev in batch.events:
        try:
            r = _write_one(ev)
            results.append({"event_id": r["event_id"], "accepted": True,
                            "duplicate": r["duplicate"], "durable": True,
                            "project_revision": r["project_revision"]})
        except HTTPException as e:
            results.append({"event_id": ev.event_id, "accepted": False,
                            "durable": False, "error": e.detail})
    return {"results": results}


@app.get("/v1/events/{event_id}", dependencies=[Depends(require_auth)])
async def get_event(event_id: str):
    ev = STATE["pg"].get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="event not found")
    # large payload: inline metadata + blob ref (blob content via separate fetch)
    return ev


# ---------- projects ----------

@app.get("/v1/projects", dependencies=[Depends(require_auth)])
async def list_projects():
    return {"projects": STATE["pg"].list_projects()}


@app.post("/v1/projects", dependencies=[Depends(require_auth)])
async def create_project(p: ProjectIn):
    if p.status not in ("ACTIVE", "PAUSED", "COMPLETED", "ARCHIVED"):
        raise HTTPException(status_code=400, detail="invalid status")
    created = STATE["pg"].create_project(p.project_id, p.name, p.status)
    STATE["ram"].invalidate(p.project_id)
    return created


@app.get("/v1/projects/{project_id}", dependencies=[Depends(require_auth)])
async def get_project(project_id: str):
    p = STATE["pg"].get_project(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
    return p


@app.get("/v1/projects/{project_id}/state", dependencies=[Depends(require_auth)])
async def get_project_state(project_id: str):
    pg = STATE["pg"]
    if pg.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    items = pg.current_memory(project_id)
    proj = pg.get_project(project_id)
    return {
        "project": proj,
        "revision": proj["revision"],
        "decisions": [i for i in items if i["kind"] == "DECISION"],
        "constraints": [i for i in items if i["kind"] == "CONSTRAINT"],
        "tasks": [i for i in items if i["kind"] == "TASK"],
        "event_count": pg.count_events(project_id),
    }


# ---------- memory ----------

@app.post("/v1/resolve-project", dependencies=[Depends(require_auth)])
async def resolve_project(r: ResolveIn):
    return STATE["resolver"].resolve(project_id=r.project_id,
                                     session_id=r.session_id, query=r.query,
                                     source=r.source, profile=r.profile, channel=r.channel,
                                     parent_session_id=r.parent_session_id,
                                     conversation_id=r.conversation_id)


@app.post("/v1/memory/context", dependencies=[Depends(require_auth)])
async def memory_context(c: ContextIn):
    # resolve project if not explicit
    if not c.project_id:
        r = STATE["resolver"].resolve(session_id=c.session_id, query=c.query)
        c.project_id = r["project_id"]
        if not c.project_id:
            raise HTTPException(status_code=404, detail="project not resolved")
    elif STATE["pg"].get_project(c.project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    return STATE["capsules"].build(c.project_id, token_budget=c.token_budget,
                                   since_revision=c.since_revision)


# ---------- M4: hybrid memory search ----------

from retrieval.hybrid import HybridRetriever, select_mode

_retriever: HybridRetriever | None = None


def _get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever(STATE["cfg"])
    return _retriever


class MemorySearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None
    session_id: str | None = None
    mode: str | None = None          # NONE|HOT|WARM|DEEP; absent = deterministic
    limit: int = Field(default=10, ge=1, le=50)
    at_time: str | None = None       # ISO timestamp: historical query


@app.post("/v1/memory/search", dependencies=[Depends(require_auth)])
async def memory_search(s: MemorySearchIn):
    try:
        mode = select_mode(s.query, s.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if mode == "NONE":
        return {"query": s.query, "mode": "NONE", "count": 0, "results": [],
                "memory_disabled": True}

    if mode == "HOT":
        # structured current state only: resolve project, return hot capsule
        if not s.project_id:
            r = STATE["resolver"].resolve(session_id=s.session_id, query=s.query)
            s.project_id = r["project_id"]
        if not s.project_id:
            return {"query": s.query, "mode": "HOT", "count": 0, "results": [],
                    "note": "project not resolved; no hot state"}
        if STATE["pg"].get_project(s.project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        cap = STATE["capsules"].build(s.project_id, token_budget=2000)
        return {"query": s.query, "mode": "HOT",
                "project_id": s.project_id,
                "capsule": cap, "retrieval_source": "HOT"}

    # WARM / DEEP
    res = _get_retriever().search(
        query=s.query, mode=mode, limit=s.limit,
        project_id=s.project_id, session_id=s.session_id,
        at_time=s.at_time)
    return res


# ---------- history (M2) ----------

@app.get("/v1/sessions", dependencies=[Depends(require_auth)])
async def list_sessions(limit: int = 50, cursor: str | None = None,
                        source: str | None = None):
    pg = STATE["pg"]
    with pg.conn.cursor() as cur:
        q = """SELECT megabrain_session_id, source_system, source_session_id,
                      started_at, ended_at, project_id, event_count, ingested_at
               FROM import_sessions"""
        cond, args = [], []
        if cursor:
            cond.append("megabrain_session_id > %s"); args.append(cursor)
        if source:
            cond.append("source_system = %s"); args.append(source)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY megabrain_session_id LIMIT %s"
        args.append(min(limit, 200))
        cur.execute(q, args)
        rows = [dict(zip(("session_id", "source_system", "source_session_id",
                          "started_at", "ended_at", "project_id", "event_count",
                          "ingested_at"), r)) for r in cur.fetchall()]
    return {"sessions": rows, "next_cursor": rows[-1]["session_id"] if len(rows) == min(limit, 200) else None}


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def get_session(session_id: str):
    pg = STATE["pg"]
    with pg.conn.cursor() as cur:
        cur.execute("""SELECT megabrain_session_id, source_system, source_session_id,
                              started_at, ended_at, project_id, metadata, event_count,
                              ingested_at FROM import_sessions WHERE megabrain_session_id=%s""",
                    (session_id,))
        r = cur.fetchone()
        if r is None:
            raise HTTPException(status_code=404, detail="session not found")
        return dict(zip(("session_id", "source_system", "source_session_id",
                         "started_at", "ended_at", "project_id", "metadata",
                         "event_count", "ingested_at"), r))


@app.get("/v1/sessions/{session_id}/events", dependencies=[Depends(require_auth)])
async def session_events(session_id: str, limit: int = 100, before: str | None = None,
                         event_type: str | None = None):
    """Full conversation timeline. Deterministic order:
    (original_sequence, created_at, event_id)."""
    pg = STATE["pg"]
    with pg.conn.cursor() as cur:
        cur.execute("SELECT 1 FROM import_sessions WHERE megabrain_session_id=%s", (session_id,))
        if cur.fetchone() is None:
            # also allow direct event session_id (live sessions)
            cur.execute("SELECT 1 FROM events WHERE session_id=%s LIMIT 1", (session_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="session not found")
        q = """SELECT event_id, event_type, created_at, source_created_at, sequence,
                      original_sequence, payload, payload_hash, blob_ref, blob_size,
                      model, provider, metadata, source, source_session_id
               FROM events WHERE session_id=%s"""
        args = [session_id]
        if before:
            q += " AND (coalesce(original_sequence, sequence), event_id) < (%s, %s)"
            cur.execute("SELECT coalesce(original_sequence, sequence) FROM events WHERE event_id=%s", (before,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="cursor event not found")
            args.extend([row[0], before])
        if event_type:
            q += " AND event_type=%s"; args.append(event_type)
        q += """ ORDER BY coalesce(original_sequence, sequence) ASC, created_at ASC, event_id ASC
                LIMIT %s"""
        args.append(min(limit, 500))
        cur.execute(q, args)
        rows = [dict(zip(("event_id", "event_type", "created_at", "source_created_at",
                          "sequence", "original_sequence", "payload", "payload_hash",
                          "blob_ref", "blob_size", "model", "provider", "metadata",
                          "source", "source_session_id"), r)) for r in cur.fetchall()]
    return {"session_id": session_id, "events": rows,
            "next_cursor": rows[-1]["event_id"] if len(rows) == min(limit, 500) else None}


@app.get("/v1/sessions/{session_id}/timeline", dependencies=[Depends(require_auth)])
async def session_timeline(session_id: str, limit: int = 100, before: str | None = None):
    """Alias of /events with same pagination (full reconstruction entrypoint)."""
    return await session_events(session_id, limit=limit, before=before)


@app.get("/v1/projects/{project_id}/events", dependencies=[Depends(require_auth)])
async def project_events(project_id: str, limit: int = 100, before: str | None = None,
                         event_type: str | None = None,
                         from_ts: str | None = None, to_ts: str | None = None):
    pg = STATE["pg"]
    with pg.conn.cursor() as cur:
        q = """SELECT event_id, session_id, event_type, created_at, source_created_at,
                      sequence, original_sequence, payload_hash, blob_ref, source
               FROM events WHERE project_id=%s"""
        args = [project_id]
        if before:
            q += " AND (created_at, event_id) < (%s, %s)"
            cur.execute("SELECT created_at FROM events WHERE event_id=%s", (before,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="cursor event not found")
            args.extend([row[0], before])
        if event_type:
            q += " AND event_type=%s"; args.append(event_type)
        if from_ts:
            q += " AND created_at >= %s"; args.append(from_ts)
        if to_ts:
            q += " AND created_at <= %s"; args.append(to_ts)
        q += " ORDER BY created_at DESC, event_id DESC LIMIT %s"
        args.append(min(limit, 500))
        cur.execute(q, args)
        rows = [dict(zip(("event_id", "session_id", "event_type", "created_at",
                          "source_created_at", "sequence", "original_sequence",
                          "payload_hash", "blob_ref", "source"), r)) for r in cur.fetchall()]
    return {"project_id": project_id, "events": rows,
            "next_cursor": rows[-1]["event_id"] if len(rows) == min(limit, 500) else None}


@app.get("/v1/search", dependencies=[Depends(require_auth)])
async def search_events(q: str | None = None, source_record_id: str | None = None,
                        session_id: str | None = None, project_id: str | None = None,
                        event_type: str | None = None, source: str | None = None,
                        limit: int = 50):
    """Exact + lightweight FTS search. No vector search."""
    pg = STATE["pg"]
    with pg.conn.cursor() as cur:
        if source_record_id and source:
            cur.execute("""SELECT sr.source_system, sr.source_record_id, sr.megabrain_event_id,
                                  sr.match_type, sr.ingested_at
                           FROM source_records sr WHERE sr.source_system=%s
                             AND sr.source_record_id=%s""", (source, source_record_id))
            rows = [dict(zip(("source_system", "source_record_id", "event_id",
                              "match_type", "ingested_at"), r)) for r in cur.fetchall()]
            return {"mode": "exact", "results": rows}
        if not q:
            raise HTTPException(status_code=400, detail="q or source_record_id required")
        sql = """SELECT event_id, session_id, project_id, event_type, created_at, source
                 FROM events WHERE fts @@ websearch_to_tsquery('simple', %s)"""
        args = [q]
        if session_id:
            sql += " AND session_id=%s"; args.append(session_id)
        if project_id:
            sql += " AND project_id=%s"; args.append(project_id)
        if event_type:
            sql += " AND event_type=%s"; args.append(event_type)
        if source:
            sql += " AND source=%s"; args.append(source)
        sql += " ORDER BY created_at DESC LIMIT %s"
        args.append(min(limit, 200))
        cur.execute(sql, args)
        rows = [dict(zip(("event_id", "session_id", "project_id", "event_type",
                          "created_at", "source"), r)) for r in cur.fetchall()]
    return {"mode": "fts", "results": rows}


@app.get("/v1/blobs/{blob_ref}", dependencies=[Depends(require_auth)])
async def get_blob(blob_ref: str):
    data = STATE["blobs"].get(blob_ref)
    if data is None:
        raise HTTPException(status_code=404, detail="blob not found")
    from fastapi import Response
    return Response(content=data, media_type="application/json")


@app.get("/v1/sessions/{session_id}/project", dependencies=[Depends(require_auth)])
async def session_project(session_id: str):
    pid = STATE["redis"].get_str(f"session:{session_id}")
    source = "redis"
    if not pid:
        pid = STATE["pg"].get_session_project(session_id)
        source = "postgres"
    if not pid:
        raise HTTPException(status_code=404, detail="session not mapped")
    return {"session_id": session_id, "project_id": pid, "source": source}
