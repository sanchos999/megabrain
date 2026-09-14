"""Project-scoped, durable and debounced background consolidation.

The worker polls every minute, but inference is allowed only after the batch
threshold or maximum wait. PostgreSQL is the scheduler source of truth.
Router is contacted only through HTTP with model=main-auto and SUMMARIZE.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from core.config import load_config
from storage.pg import EXPERIENCE_KINDS, Postgres

ROUTER_URL = os.environ.get("MEGABRAIN_ROUTER_URL", "http://127.0.0.1:4100")
ROUTER_API_KEY = os.environ.get("MODEL_ROUTER_API_KEY", "")
MODEL = "main-auto"
TASK_CLASS = "SUMMARIZE"
PROFILE = "CHEAPEST"
REQUIRED_CAPABILITIES = ["summarization"]
OUTPUT_RESERVE_TOKENS = 2000
MEANINGFUL = {"USER_MESSAGE", "ASSISTANT_MESSAGE", "ERROR", "TEST_RESULT", "FILE_WRITE", "SHELL_RESULT"}
NOISE = {"ok", "okay", "thanks", "thank you", "done", "понял", "спасибо"}
BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)


@dataclass(frozen=True)
class Settings:
    min_batch_events: int = int(os.environ.get("MB_CONSOLIDATION_MIN_BATCH_EVENTS", "20"))
    max_wait_s: int = int(os.environ.get("MB_CONSOLIDATION_MAX_WAIT_S", "1800"))
    cooldown_s: int = int(os.environ.get("MB_CONSOLIDATION_COOLDOWN_S", "900"))
    max_batch_events: int = int(os.environ.get("MB_CONSOLIDATION_MAX_BATCH_EVENTS", "100"))
    max_batch_tokens: int = int(os.environ.get("MB_CONSOLIDATION_MAX_BATCH_TOKENS", "12000"))
    max_cost_call: float = float(os.environ.get("MB_CONSOLIDATION_MAX_COST_PER_CALL", "0.50"))
    daily_budget: float = float(os.environ.get("MB_CONSOLIDATION_DAILY_BUDGET_USD", "25"))
    monthly_budget: float = float(os.environ.get("MB_CONSOLIDATION_MONTHLY_BUDGET_USD", "250"))
    max_calls_hour: int = int(os.environ.get("MB_CONSOLIDATION_MAX_CALLS_PER_HOUR", "2"))
    max_calls_day: int = int(os.environ.get("MB_CONSOLIDATION_MAX_CALLS_PER_DAY", "24"))
    project_scope: str | None = os.environ.get("MB_CONSOLIDATION_PROJECT_SCOPE") or None


def _text(event: dict) -> str:
    payload = event.get("payload") or {}
    return str(payload.get("text") or payload.get("content") or "").strip()


def prefilter(events: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for event in events:
        text = _text(event)
        if event.get("event_id") in seen or event.get("event_type") not in MEANINGFUL:
            continue
        if len(text) < 12 or text.casefold() in NOISE:
            continue
        if (event.get("metadata") or {}).get("technical_echo"):
            continue
        seen.add(event["event_id"])
        result.append(event)
    return result


def batch_id(project_id: str, events: list[dict]) -> tuple[str, str]:
    source = "|".join(event["event_id"] for event in events)
    digest = hashlib.sha256(f"{project_id}|{source}".encode()).hexdigest()
    return f"batch_{digest}", digest


def estimate_tokens(events: list[dict]) -> int:
    return max(1, sum(len(_text(event)) for event in events) // 4 + 700)


def _first_json_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for pos, char in enumerate(text):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(text[pos:])
                return value if isinstance(value, dict) else None
            except ValueError:
                continue
    return None


def extract_items(events: list[dict], transport: Callable[..., tuple[dict, dict, dict]]) -> tuple[list[dict], dict]:
    ids = {event["event_id"] for event in events}
    lines = [f"[{e['event_id']}] {e['event_type']}: {_text(e)[:1200]}" for e in events]
    prompt = (
        "Extract only implicit candidate project memory. Do not confirm facts. "
        "Ignore explicit DECISION/CONSTRAINT/TASK events. Return JSON with items; "
        "kind must be PROCEDURE, FAILURE_PATTERN, EXPERIENCE or REJECTED_APPROACH; "
        "include confidence and source_event_ids from the input.\n" + "\n".join(lines)
    )
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "extra_body": {
            "task_class": TASK_CLASS,
            "profile": PROFILE,
            "required_capabilities": REQUIRED_CAPABILITIES,
            "required_context": estimate_tokens(events) + OUTPUT_RESERVE_TOKENS,
            "reserved_output": OUTPUT_RESERVE_TOKENS,
        },
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 2000,
    }
    response, headers, usage = transport(body)
    content = (response.get("choices") or [{"message": {"content": ""}}])[0]["message"].get("content", "")
    obj = _first_json_object(content) or response if isinstance(response, dict) else {}
    items: list[dict] = []
    for raw in obj.get("items") or []:
        kind = str(raw.get("kind") or "").upper()
        source_ids = [value for value in raw.get("source_event_ids") or [] if value in ids]
        if kind not in EXPERIENCE_KINDS or not source_ids:
            continue
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        items.append({
            "kind": kind,
            "content": {key: raw.get(key) or "" for key in ("title", "situation", "action", "result", "lesson")},
            "confidence": confidence,
            "source_event_ids": source_ids,
        })
    return items, {**(usage or {}), "model_selected": headers.get("x-gateway-selected-slug"), "provider": headers.get("x-gateway-selected-provider")}


class PostgresSchedulerRepository:
    def __init__(self, pg: Postgres, settings: Settings):
        self.pg, self.settings = pg, settings

    def eligible_project(self) -> str | None:
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT project_id FROM consolidation_projects
                WHERE pending_event_count > 0 AND NOT paused AND NOT budget_paused
                  AND next_eligible_at <= now()
                  AND (pending_event_count >= %s OR first_dirty_at <= now() - (%s || ' seconds')::interval)
                  AND (%s::text IS NULL OR project_id=%s::text)
                ORDER BY first_dirty_at, project_id
                FOR UPDATE SKIP LOCKED LIMIT 1""",
                (self.settings.min_batch_events, self.settings.max_wait_s, self.settings.project_scope, self.settings.project_scope))
            row = cur.fetchone()
            self.pg.conn.commit()
            return row[0] if row else None

    def eligible_projects(self) -> list[str]:
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT project_id FROM consolidation_projects
                WHERE pending_event_count > 0 AND NOT paused AND NOT budget_paused
                  AND next_eligible_at <= now()
                  AND (pending_event_count >= %s OR first_dirty_at <= now() - (%s || ' seconds')::interval)
                  AND (%s::text IS NULL OR project_id=%s::text)
                ORDER BY first_dirty_at, project_id""", (self.settings.min_batch_events, self.settings.max_wait_s, self.settings.project_scope, self.settings.project_scope))
            return [row[0] for row in cur.fetchall()]

    def events(self, project_id: str) -> list[dict]:
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT e.event_id,e.project_id,e.event_type,e.created_at,e.payload,e.metadata
                FROM events e JOIN consolidation_projects c ON c.project_id=e.project_id
                WHERE e.project_id=%s AND e.event_type=ANY(%s)
                  AND (c.last_consolidated_event_id IS NULL OR (e.created_at,e.event_id) >
                    (SELECT created_at,event_id FROM events WHERE event_id=c.last_consolidated_event_id))
                ORDER BY e.created_at,e.event_id LIMIT %s""",
                (project_id, list(MEANINGFUL), self.settings.max_batch_events))
            columns = [item.name for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def rate_guard(self) -> tuple[bool, str | None]:
        with self.pg.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('megabrain:consolidation:dispatch', 0))")
            cur.execute("SELECT count(*) FROM consolidation_runs WHERE started_at >= now()-interval '1 hour' AND status IN ('dispatching','success','failed')")
            hour = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM consolidation_runs WHERE started_at >= current_date AND status IN ('dispatching','success','failed')")
            day = int(cur.fetchone()[0])
            if hour >= self.settings.max_calls_hour:
                return False, "RATE_LIMIT_HOURLY"
            if day >= self.settings.max_calls_day:
                return False, "RATE_LIMIT_DAILY"
        return True, None

    def reserve_dispatch(self, project_id: str, events: list[dict], digest: str) -> tuple[bool, str | None]:
        """Reserve the billable dispatch under a transaction/advisory lock."""
        with self.pg.conn.transaction():
            ok, reason = self.rate_guard()
            if not ok:
                with self.pg.conn.cursor() as cur:
                    cur.execute("UPDATE consolidation_projects SET rate_paused=true,last_block_reason=%s,next_eligible_at=now()+interval '1 minute' WHERE project_id=%s", (reason, project_id))
                return False, reason
            batch, _ = batch_id(project_id, events)
            with self.pg.conn.cursor() as cur:
                cur.execute("""INSERT INTO consolidation_runs
                    (run_id,project_id,batch_id,from_event_id,to_event_id,event_count,input_hash,model_requested,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'dispatching')
                    ON CONFLICT (project_id,batch_id) DO NOTHING""",
                    ("run_" + uuid.uuid4().hex, project_id, batch, events[0]["event_id"], events[-1]["event_id"], len(events), digest, MODEL))
                if cur.rowcount != 1:
                    return False, "DUPLICATE_BATCH"
        return True, None
    def budget(self) -> tuple[float, float]:
        with self.pg.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(sum(estimated_cost),0) FROM consolidation_runs WHERE started_at >= current_date AND status IN ('success','failed','dispatching')")
            daily = float(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(sum(estimated_cost),0) FROM consolidation_runs WHERE started_at >= date_trunc('month', now()) AND status IN ('success','failed','dispatching')")
            monthly = float(cur.fetchone()[0])
            return daily, monthly


    def duplicate(self, project_id: str, digest: str) -> bool:
        with self.pg.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM consolidation_runs WHERE project_id=%s AND input_hash=%s", (project_id, digest))
            return cur.fetchone() is not None

    def record(self, project_id: str, events: list[dict], digest: str, status: str, **values: Any) -> None:
        batch, _ = batch_id(project_id, events)
        with self.pg.conn.cursor() as cur:
            cur.execute("""INSERT INTO consolidation_runs
                (run_id,project_id,batch_id,from_event_id,to_event_id,event_count,input_hash,model_requested,
                 model_selected,provider,finished_at,status,tokens_in,tokens_out,estimated_cost,derived_items_count,retry_count,error_class)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (project_id,batch_id) DO UPDATE SET status=EXCLUDED.status,finished_at=now(),
                    model_selected=EXCLUDED.model_selected,provider=EXCLUDED.provider,error_class=EXCLUDED.error_class""",
                ("run_" + uuid.uuid4().hex, project_id, batch, events[0]["event_id"], events[-1]["event_id"], len(events), digest,
                 MODEL, values.get("model_selected"), values.get("provider"), status, int(values.get("tokens_in", 0)),
                 int(values.get("tokens_out", 0)), float(values.get("estimated_cost", 0)), int(values.get("derived_items_count", 0)),
                 int(values.get("retry_count", 0)), values.get("error_class")))
            self.pg.conn.commit()

    def success(self, project_id: str, events: list[dict]) -> None:
        with self.pg.conn.cursor() as cur:
            cur.execute("""UPDATE consolidation_projects SET last_consolidated_event_id=%s,
                pending_event_count=GREATEST(0,pending_event_count-%s), retry_count=0,last_error=NULL,
                last_success_at=now(),next_eligible_at=now()+(%s || ' seconds')::interval,updated_at=now()
                WHERE project_id=%s""", (events[-1]["event_id"], len(events), self.settings.cooldown_s, project_id))
            self.pg.conn.commit()

    def failure(self, project_id: str, error: Exception, retry: int) -> None:
        delay = BACKOFF_SECONDS[min(retry, len(BACKOFF_SECONDS) - 1)]
        with self.pg.conn.cursor() as cur:
            cur.execute("""UPDATE consolidation_projects SET retry_count=retry_count+1,last_error_at=now(),last_error=%s,
                next_eligible_at=now()+(%s || ' seconds')::interval,updated_at=now() WHERE project_id=%s""",
                (str(error)[:500], delay, project_id))
            self.pg.conn.commit()


class ConsolidationWorker:
    def __init__(self, repository: PostgresSchedulerRepository | None = None,
                 transport: Callable[[dict], tuple[dict, dict, dict]] | None = None,
                 settings: Settings | None = None):
        self.settings = settings or Settings()
        if repository is None:
            cfg = load_config()
            repository = PostgresSchedulerRepository(Postgres(cfg["postgres_dsn"], None, cfg["blob_inline_limit"]), self.settings)
        self.repository = repository
        self.transport = transport or self._route_request

    def _route_request(self, body: dict) -> tuple[dict, dict, dict]:
        """Production transport: Router selector semantics only; no direct fallback."""
        request = urllib.request.Request(
            ROUTER_URL + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-hermes-task-class": TASK_CLASS},
            method="POST",
        )
        if ROUTER_API_KEY:
            request.add_header("Authorization", "Bearer " + ROUTER_API_KEY)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
                return payload, dict(response.headers), payload.get("usage") or {}
        except urllib.error.HTTPError as error:
            if error.code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"router_retryable_http_{error.code}") from error
            raise

    def dry_run(self) -> dict:
        projects = self.repository.eligible_projects()
        planned = []
        for project in projects:
            events = prefilter(self.repository.events(project))
            if events:
                batches = (len(events) + self.settings.max_batch_events - 1) // self.settings.max_batch_events
                planned.append({"project_id": project, "events": len(events), "batches": batches, "estimated_tokens": sum(estimate_tokens(events[i:i+self.settings.max_batch_events]) for i in range(0, len(events), self.settings.max_batch_events))})
        return {"status": "plan", "eligible_projects": len(projects), "planned_batches": sum(item["batches"] for item in planned), "planned_llm_calls": sum(item["batches"] for item in planned), "estimated_input_tokens": sum(item["estimated_tokens"] for item in planned), "projects": planned, "model": MODEL, "task_class": TASK_CLASS, "profile": PROFILE, "llm_calls": 0}

    def run_once(self) -> dict:
        project = self.repository.eligible_project()
        if not project:
            return {"status": "idle", "llm_calls": 0}
        events = prefilter(self.repository.events(project))
        if not events:
            return {"status": "no_llm", "llm_calls": 0}
        events = events[:self.settings.max_batch_events]
        tokens = estimate_tokens(events)
        if tokens > self.settings.max_batch_tokens:
            while len(events) > 1 and estimate_tokens(events) > self.settings.max_batch_tokens:
                events.pop()
        batch, digest = batch_id(project, events)
        if self.repository.duplicate(project, digest):
            self.repository.success(project, events)
            return {"status": "duplicate", "llm_calls": 0, "batch_id": batch}
        daily, monthly = self.repository.budget()
        estimated_cost = float(os.environ.get("MB_CONSOLIDATION_ESTIMATED_COST_PER_1K", "0")) * estimate_tokens(events) / 1000
        if estimated_cost > self.settings.max_cost_call or daily + estimated_cost > self.settings.daily_budget or monthly + estimated_cost > self.settings.monthly_budget:
            self.repository.record(project, events, digest, "budget_paused", estimated_cost=estimated_cost)
            return {"status": "budget_paused", "llm_calls": 0, "batch_id": batch}
        allowed, reason = self.repository.reserve_dispatch(project, events, digest)
        if not allowed:
            return {"status": "blocked", "reason": reason, "llm_calls": 0, "batch_id": batch}
        try:
            items, usage = extract_items(events, self.transport)
            for item in items:
                self.repository.pg.add_derived_item(project, item["kind"], item["content"], item["source_event_ids"], confidence=item["confidence"], extractor="LLM", extractor_version="megabrain-0.1.1:main-auto")
            self.repository.record(project, events, digest, "success", model_selected=usage.get("model_selected"), provider=usage.get("provider"), tokens_in=usage.get("prompt_tokens", 0), tokens_out=usage.get("completion_tokens", 0), estimated_cost=estimated_cost, derived_items_count=len(items))
            self.repository.success(project, events)
            return {"status": "success", "llm_calls": 1, "batch_id": batch, "events": len(events), "items": len(items), **usage}
        except Exception as error:  # watermark intentionally remains unchanged
            self.repository.failure(project, error, 0)
            self.repository.record(project, events, digest, "failed", error_class=type(error).__name__)
            return {"status": "failed", "llm_calls": 1, "error": type(error).__name__}
