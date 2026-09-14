"""Project-scoped, durable and debounced background consolidation.

The worker polls every minute, but inference is allowed only after the
adaptive idle window for the pending batch class (quality first: cost is
saved on duplicate/trivial/small-batch dispatches, never on extraction
quality). PostgreSQL is the scheduler source of truth. Router is contacted
only through HTTP with model=main-auto and SUMMARIZE semantics.

0.1.2 invariants:
- one LLM batch = ONE project (never merged across projects);
- derived items + run record + watermark commit in ONE transaction;
- tokens/cost are REPORTED / ESTIMATED / UNKNOWN, never fabricated zeros;
- daily input-token guard with MEMORY_QUALITY_THROTTLED visibility;
- worker-level circuit breaker (3 consecutive failures -> BACKOFF -> HALF_OPEN);
- two-stage quality extraction: cheap stage first, stronger stage only on
  escalation triggers (parse failure / low confidence / important-content loss).
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Callable

from core.config import load_config
from operations import WorkerHeartbeat
from storage.pg import EXPERIENCE_KINDS, Postgres

ROUTER_URL = os.environ.get("MEGABRAIN_ROUTER_URL", "http://127.0.0.1:4100")
INFERENCE_GATEWAY_API_KEY = os.environ.get("INFERENCE_GATEWAY_API_KEY", "")
MODEL = "main-auto"
TASK_CLASS = "SUMMARIZE"
PROFILE = "CHEAPEST"
STAGE2_PROFILE = os.environ.get("MB_CONSOLIDATION_STAGE2_PROFILE", "QUALITY")
REQUIRED_CAPABILITIES = ["summarization"]
OUTPUT_RESERVE_TOKENS = 2000
MEANINGFUL = {"USER_MESSAGE", "ASSISTANT_MESSAGE", "ERROR", "TEST_RESULT", "FILE_WRITE", "SHELL_RESULT"}
NOISE = {"ok", "okay", "thanks", "thank you", "done", "понял", "спасибо"}
BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)
IMPORTANCE_LEVELS = {"LOW", "NORMAL", "HIGH", "CRITICAL"}
# Pre-LLM scheduling heuristic only (fast path); LLM-assigned importance is
# validated separately. Two distinct marker hits are required, never one word.
IMPORTANCE_MARKERS = (
    "решение", "решено", "ограничение", "задача", "architecture", "архитектур",
    "root cause", "корневая причина", "lesson", "урок", "security", "безопасност",
    "constraint", "deadline", "дедлайн", "migrate", "миграц", "incident", "инцидент",
    "regression", "регресс", "freeze", "заморожен", "production", "продакш",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Adaptive batching (idle seconds measured from last_dirty_at).
    idle_s_important: int = _env_int("MB_CONSOLIDATION_IDLE_S_IMPORTANT", 900)      # 5-15 min: HIGH/CRITICAL fast path
    idle_s_large: int = _env_int("MB_CONSOLIDATION_IDLE_S_LARGE", 1800)             # 20+ meaningful events
    idle_s_medium: int = _env_int("MB_CONSOLIDATION_IDLE_S_MEDIUM", 7200)           # 5-19 events
    idle_s_small: int = _env_int("MB_CONSOLIDATION_IDLE_S_SMALL", 43200)            # 1-4 events
    abs_max_wait_s: int = _env_int("MB_CONSOLIDATION_ABS_MAX_WAIT_S", 86400)        # 24h absolute cap
    large_n: int = _env_int("MB_CONSOLIDATION_LARGE_N", 20)
    medium_n: int = _env_int("MB_CONSOLIDATION_MEDIUM_N", 5)
    max_wait_s: int = _env_int("MB_CONSOLIDATION_MAX_WAIT_S", 1800)                 # legacy knob, abs cap fallback
    cooldown_s: int = _env_int("MB_CONSOLIDATION_COOLDOWN_S", 900)
    max_batch_events: int = _env_int("MB_CONSOLIDATION_MAX_BATCH_EVENTS", 100)
    max_batch_tokens: int = _env_int("MB_CONSOLIDATION_MAX_BATCH_TOKENS", 12000)
    max_input_tokens_per_day: int = _env_int("MB_CONSOLIDATION_MAX_INPUT_TOKENS_PER_DAY", 100000)
    max_cost_call: float = float(os.environ.get("MB_CONSOLIDATION_MAX_COST_PER_CALL", "0.50"))
    daily_budget: float = float(os.environ.get("MB_CONSOLIDATION_DAILY_BUDGET_USD", "25"))
    monthly_budget: float = float(os.environ.get("MB_CONSOLIDATION_MONTHLY_BUDGET_USD", "250"))
    max_calls_hour: int = _env_int("MB_CONSOLIDATION_MAX_CALLS_PER_HOUR", 2)
    max_calls_day: int = _env_int("MB_CONSOLIDATION_MAX_CALLS_PER_DAY", 24)
    escalate_confidence: float = float(os.environ.get("MB_CONSOLIDATION_ESCALATE_CONFIDENCE", "0.60"))
    breaker_threshold: int = _env_int("MB_CONSOLIDATION_BREAKER_THRESHOLD", 3)
    breaker_cooldown_s: int = _env_int("MB_CONSOLIDATION_BREAKER_COOLDOWN_S", 1800)
    project_scope: str | None = os.environ.get("MB_CONSOLIDATION_PROJECT_SCOPE") or None


def _text(event: dict) -> str:
    payload = event.get("payload") or {}
    return str(payload.get("text") or payload.get("content") or "").strip()


def importance_signal(text: str) -> int:
    """Cheap pre-LLM importance signal: count distinct marker hits (never one word alone)."""
    lowered = text.lower()
    return len({marker for marker in IMPORTANCE_MARKERS if marker in lowered})


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


def _build_body(events: list[dict], profile: str, max_tokens: int) -> dict:
    lines = [f"[{e['event_id']}] {e['event_type']}: {_text(e)[:1200]}" for e in events]
    prompt = (
        "Extract only implicit candidate project memory. Do not confirm facts. "
        "Ignore explicit DECISION/CONSTRAINT/TASK events. Return JSON with items; "
        "kind must be PROCEDURE, FAILURE_PATTERN, EXPERIENCE or REJECTED_APPROACH; "
        "importance must be LOW, NORMAL, HIGH or CRITICAL (decisions, constraints, "
        "architecture, root causes, failures and lessons are HIGH or CRITICAL); "
        "include confidence in [0,1] and source_event_ids from the input. "
        "When unsure, lower the confidence instead of inventing facts.\n" + "\n".join(lines)
    )
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "extra_body": {
            "task_class": TASK_CLASS,
            "profile": profile,
            "required_capabilities": REQUIRED_CAPABILITIES,
            "required_context": estimate_tokens(events) + OUTPUT_RESERVE_TOKENS,
            "reserved_output": OUTPUT_RESERVE_TOKENS,
        },
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": max_tokens,
    }


def _parse_items(raw_obj: dict | None, ids: set[str]) -> list[dict]:
    items: list[dict] = []
    for raw in (raw_obj or {}).get("items") or []:
        kind = str(raw.get("kind") or "").upper()
        source_ids = [value for value in raw.get("source_event_ids") or [] if value in ids]
        if kind not in EXPERIENCE_KINDS or not source_ids:
            continue
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        importance = str(raw.get("importance") or "").upper()
        if importance not in IMPORTANCE_LEVELS:
            importance = "NORMAL"
        items.append({
            "kind": kind,
            "content": {key: raw.get(key) or "" for key in ("title", "situation", "action", "result", "lesson")},
            "confidence": confidence,
            "importance": importance,
            "source_event_ids": source_ids,
        })
    return items


def extract_items(events: list[dict], transport: Callable[..., tuple[dict, dict, dict]],
                  profile: str = PROFILE, max_tokens: int = 2000) -> tuple[list[dict], dict]:
    """Single-stage extraction (kept for tests and stage-1 reuse)."""
    response, headers, usage = transport(_build_body(events, profile, max_tokens))
    content = (response.get("choices") or [{"message": {"content": ""}}])[0]["message"].get("content", "")
    obj = _first_json_object(content) or (response if isinstance(response, dict) and "items" in response else None)
    return _parse_items(obj, {event["event_id"] for event in events}), {
        **(usage or {}),
        "parsed": obj is not None,
        "model_selected": headers.get("x-gateway-selected-slug"),
        "provider": headers.get("x-gateway-selected-provider"),
    }


class PostgresSchedulerRepository:
    def __init__(self, pg: Postgres, settings: Settings):
        self.pg, self.settings = pg, settings

    def candidate_projects(self) -> list[dict]:
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT project_id,first_dirty_at,last_dirty_at,pending_event_count
                FROM consolidation_projects
                WHERE pending_event_count > 0 AND NOT paused AND NOT budget_paused
                  AND next_eligible_at <= now()
                  AND (%s::text IS NULL OR project_id=%s::text)
                ORDER BY first_dirty_at, project_id""",
                (self.settings.project_scope, self.settings.project_scope))
            columns = [item.name for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def has_important_pending(self, project_id: str) -> bool:
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM events e JOIN consolidation_projects c
                  ON c.project_id=e.project_id
                WHERE e.project_id=%s
                  AND (c.last_consolidated_event_id IS NULL OR (e.created_at,e.event_id) >
                    (SELECT created_at,event_id FROM events WHERE event_id=c.last_consolidated_event_id))
                  AND e.event_type=ANY(%s)
                  AND (e.payload::text ILIKE ANY(%s) OR e.metadata::text ILIKE ANY(%s))
                LIMIT 1""",
                (project_id, list(MEANINGFUL),
                 [f"%{m}%" for m in IMPORTANCE_MARKERS], [f"%{m}%" for m in IMPORTANCE_MARKERS]))
            return cur.fetchone() is not None

    def eligible_project(self) -> str | None:
        """Adaptive eligibility: idle window by pending class, importance fast path,
        absolute max wait override. One project at a time (project isolation)."""
        for row in self.candidate_projects():
            age_dirty = (row["last_dirty_at"] and _age_s(row["last_dirty_at"])) or 0.0
            age_first = _age_s(row["first_dirty_at"])
            count = int(row["pending_event_count"])
            s = self.settings
            important = age_dirty >= s.idle_s_important and self.has_important_pending(row["project_id"])
            if eligibility(count, age_dirty, age_first, important, s):
                with self.pg.conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (row["project_id"],))
                self.pg.conn.commit()
                return row["project_id"]
        return None

    def eligible_projects(self) -> list[str]:
        """Dry-run planning uses the same adaptive policy."""
        result = []
        for row in self.candidate_projects():
            age_dirty = (row["last_dirty_at"] and _age_s(row["last_dirty_at"])) or 0.0
            age_first = _age_s(row["first_dirty_at"])
            count = int(row["pending_event_count"])
            important = age_dirty >= s_idle(self.settings) and self.has_important_pending(row["project_id"])
            if eligibility(count, age_dirty, age_first, important, self.settings):
                result.append(row["project_id"])
        return result

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

    def explicit_source_ids(self, project_id: str) -> set[str]:
        """Source ids already materialized as explicit memory (dedup vs consolidation)."""
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT source_event_ids FROM memory_items
                WHERE project_id=%s AND extractor_type='EXPLICIT' AND valid_to IS NULL""",
                (project_id,))
            found: set[str] = set()
            for row in cur.fetchall():
                found.update(row[0] or [])
            return found

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

    def daily_input_tokens_used(self) -> int:
        with self.pg.conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(sum(COALESCE(tokens_in,tokens_in_estimated,0)),0)
                FROM consolidation_runs
                WHERE started_at >= current_date AND status IN ('dispatching','success')""")
            return int(cur.fetchone()[0])

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

    def record(self, project_id: str, events: list[dict], digest: str, status: str, *, commit: bool = True, **values: Any) -> None:
        batch, _ = batch_id(project_id, events)
        with self.pg.conn.cursor() as cur:
            cur.execute("""INSERT INTO consolidation_runs
                (run_id,project_id,batch_id,from_event_id,to_event_id,event_count,input_hash,model_requested,
                 model_selected,provider,finished_at,status,tokens_in,tokens_out,estimated_cost,derived_items_count,
                 retry_count,error_class,tokens_in_status,tokens_out_status,tokens_in_estimated,tokens_out_estimated,
                 cost_status,escalation_stage,escalation_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (project_id,batch_id) DO UPDATE SET status=EXCLUDED.status,finished_at=now(),
                    model_selected=EXCLUDED.model_selected,provider=EXCLUDED.provider,error_class=EXCLUDED.error_class,
                    tokens_in=EXCLUDED.tokens_in,tokens_out=EXCLUDED.tokens_out,
                    tokens_in_status=EXCLUDED.tokens_in_status,tokens_out_status=EXCLUDED.tokens_out_status,
                    tokens_in_estimated=EXCLUDED.tokens_in_estimated,tokens_out_estimated=EXCLUDED.tokens_out_estimated,
                    cost_status=EXCLUDED.cost_status,escalation_stage=EXCLUDED.escalation_stage,
                    escalation_reason=EXCLUDED.escalation_reason""",
                ("run_" + uuid.uuid4().hex, project_id, batch, events[0]["event_id"], events[-1]["event_id"], len(events), digest,
                 MODEL, values.get("model_selected"), values.get("provider"), status,
                 values.get("tokens_in"), values.get("tokens_out"), values.get("estimated_cost"),
                 int(values.get("derived_items_count", 0)), int(values.get("retry_count", 0)), values.get("error_class"),
                 values.get("tokens_in_status", "UNKNOWN"), values.get("tokens_out_status", "UNKNOWN"),
                 values.get("tokens_in_estimated"), values.get("tokens_out_estimated"),
                 values.get("cost_status", "UNKNOWN"), int(values.get("escalation_stage", 1)),
                 values.get("escalation_reason")))
        if commit:
            self.pg.conn.commit()

    def success(self, project_id: str, events: list[dict], *, commit: bool = True) -> None:
        with self.pg.conn.cursor() as cur:
            cur.execute("""UPDATE consolidation_projects SET last_consolidated_event_id=%s,
                pending_event_count=GREATEST(0,pending_event_count-%s), retry_count=0,last_error=NULL,
                rate_paused=false,last_block_reason=NULL,
                last_success_at=now(),next_eligible_at=now()+(%s || ' seconds')::interval,updated_at=now()
                WHERE project_id=%s""", (events[-1]["event_id"] if events else None, len(events), self.settings.cooldown_s, project_id))
        if commit:
            self.pg.conn.commit()

    def defer_project(self, project_id: str, reason: str) -> None:
        with self.pg.conn.cursor() as cur:
            cur.execute("""UPDATE consolidation_projects SET last_block_reason=%s,
                next_eligible_at=date_trunc('day',now())+interval '1 day' WHERE project_id=%s""", (reason, project_id))
        self.pg.conn.commit()

    def failure(self, project_id: str, error: Exception, retry: int) -> None:
        delay = BACKOFF_SECONDS[min(retry, len(BACKOFF_SECONDS) - 1)]
        with self.pg.conn.cursor() as cur:
            cur.execute("""UPDATE consolidation_projects SET retry_count=retry_count+1,last_error_at=now(),last_error=%s,
                next_eligible_at=now()+(%s || ' seconds')::interval,updated_at=now() WHERE project_id=%s""",
                (str(error)[:500], delay, project_id))
        self.pg.conn.commit()


def s_idle(s: Settings) -> float:
    return float(s.idle_s_important)


def eligibility(count: int, age_dirty_s: float, age_first_s: float, important: bool, s: Settings) -> bool:
    """Adaptive batching policy (§21): idle window by pending class, importance
    fast path, absolute max wait override."""
    return bool(count > 0 and (
        age_first_s >= s.abs_max_wait_s
        or (count >= s.large_n and age_dirty_s >= s.idle_s_large)
        or (s.medium_n <= count < s.large_n and age_dirty_s >= s.idle_s_medium)
        or (0 < count < s.medium_n and age_dirty_s >= s.idle_s_small)
        or (important and age_dirty_s >= s.idle_s_important)
    ))


def _age_s(moment) -> float:
    import time
    from datetime import datetime
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return max(0.0, time.time() - moment.timestamp())
    return 0.0


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
        dsn = getattr(getattr(repository, "pg", None), "dsn", None)
        self.heartbeat = WorkerHeartbeat("consolidation", dsn=dsn)

    def _route_request(self, body: dict) -> tuple[dict, dict, dict]:
        """Production transport: Router selector semantics only; no direct fallback."""
        request = urllib.request.Request(
            ROUTER_URL + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-hermes-task-class": TASK_CLASS},
            method="POST",
        )
        if INFERENCE_GATEWAY_API_KEY:
            request.add_header("Authorization", "Bearer " + INFERENCE_GATEWAY_API_KEY)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
                return payload, dict(response.headers), payload.get("usage") or {}
        except urllib.error.HTTPError as error:
            if error.code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"router_retryable_http_{error.code}") from error
            raise

    def _breaker_state(self) -> tuple[str, int]:
        """Durable circuit breaker state from worker_status (consecutive_errors)."""
        try:
            with self.repository.pg.conn.cursor() as cur:
                cur.execute("SELECT consecutive_errors,extract(epoch from (now()-last_error_at)) FROM worker_status WHERE component='consolidation'")
                row = cur.fetchone()
            self.repository.pg.conn.commit()
        except Exception:
            return "CLOSED", 0
        if not row:
            return "CLOSED", 0
        consecutive, error_age = int(row[0] or 0), float(row[1] or 0)
        if consecutive >= self.settings.breaker_threshold:
            if error_age is not None and error_age < self.settings.breaker_cooldown_s:
                return "BACKOFF", consecutive
            return "HALF_OPEN", consecutive
        return "CLOSED", consecutive

    def _extract_two_stage(self, events: list[dict]) -> tuple[list[dict], dict]:
        """Stage 1: cheap eligible route. Stage 2 (stronger profile) only on triggers."""
        items, usage = extract_items(events, self.transport, PROFILE)
        stage, reason = 1, None
        important_present = any(importance_signal(_text(event)) >= 2 for event in events)
        confidences = [item["confidence"] for item in items]
        trigger = None
        if not usage.get("parsed"):
            trigger = "STRUCTURAL_VALIDATION_FAILURE"
        elif important_present and not items:
            trigger = "IMPORTANT_CONTENT_LOSS"
        elif items and confidences and sum(confidences) / len(confidences) < self.settings.escalate_confidence:
            trigger = "LOW_CONFIDENCE"
        if trigger:
            stage, reason = 2, trigger
            items2, usage2 = extract_items(events, self.transport, STAGE2_PROFILE, max_tokens=3000)
            if usage2.get("parsed") and items2:
                items, usage = items2, usage2
        return items, {**usage, "escalation_stage": stage, "escalation_reason": reason}

    def dry_run(self) -> dict:
        projects = self.repository.eligible_projects()
        planned = []
        for project in projects:
            events = prefilter(self.repository.events(project))
            events = [event for event in events if event["event_id"] not in self.repository.explicit_source_ids(project)]
            if events:
                batches = (len(events) + self.settings.max_batch_events - 1) // self.settings.max_batch_events
                planned.append({"project_id": project, "events": len(events), "batches": batches, "estimated_tokens": sum(estimate_tokens(events[i:i+self.settings.max_batch_events]) for i in range(0, len(events), self.settings.max_batch_events))})
        return {"status": "plan", "eligible_projects": len(projects), "planned_batches": sum(item["batches"] for item in planned), "planned_llm_calls": sum(item["batches"] for item in planned), "estimated_input_tokens": sum(item["estimated_tokens"] for item in planned), "projects": planned, "model": MODEL, "task_class": TASK_CLASS, "profile": PROFILE, "llm_calls": 0}

    def run_once(self) -> dict:
        self.heartbeat.update(state="RUNNING")
        breaker, consecutive = self._breaker_state()
        if breaker == "BACKOFF":
            self.heartbeat.update(state="BACKOFF", success=True, reset_errors=False, detail=f"breaker:{consecutive}")
            return {"status": "breaker_backoff", "llm_calls": 0}
        project = self.repository.eligible_project()
        if not project:
            self.heartbeat.update(state="IDLE", success=True, reset_errors=False)
            return {"status": "idle", "llm_calls": 0}
        events = prefilter(self.repository.events(project))
        explicit = self.repository.explicit_source_ids(project)
        scanned = events
        events = [event for event in events if event["event_id"] not in explicit]
        if not events:
            # Nothing meaningful: advance watermark without LLM so the queue drains.
            self.repository.success(project, scanned)
            self.heartbeat.update(state="RUNNING", success=True, reset_errors=False, processed_items=len(scanned))
            return {"status": "no_llm", "llm_calls": 0, "advanced": len(scanned)}
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
            self.heartbeat.update(state="PAUSED", success=True, reset_errors=False, detail="BUDGET")
            self.repository.record(project, events, digest, "budget_paused",
                                   estimated_cost=estimated_cost if estimated_cost else None, cost_status="UNKNOWN" if not estimated_cost else "ESTIMATED")
            return {"status": "budget_paused", "llm_calls": 0, "batch_id": batch}
        used_today = self.repository.daily_input_tokens_used()
        if used_today + estimate_tokens(events) > self.settings.max_input_tokens_per_day:
            important = self.has_important(project, events)
            state = "MEMORY_QUALITY_THROTTLED" if important else "PAUSED"
            self.heartbeat.update(state=state, success=True, reset_errors=False, detail="TOKEN_BUDGET_DAILY")
            self.repository.defer_project(project, "TOKEN_BUDGET_DAILY")
            return {"status": "token_budget_daily", "llm_calls": 0, "batch_id": batch, "important_pending": important}
        allowed, reason = self.repository.reserve_dispatch(project, events, digest)
        if not allowed:
            self.heartbeat.update(state="PAUSED", success=True, reset_errors=False, detail=reason)
            return {"status": "blocked", "reason": reason, "llm_calls": 0, "batch_id": batch}
        stage_hint = breaker  # HALF_OPEN probe is a normal single dispatch
        try:
            items, usage = self._extract_two_stage(events)
            # ONE transaction: derived items + run record + watermark (atomic).
            with self.repository.pg.conn.transaction():
                for item in items:
                    self.repository.pg.add_derived_item(
                        project, item["kind"], dict(item["content"], importance=item["importance"]),
                        item["source_event_ids"], confidence=item["confidence"],
                        extractor="LLM", extractor_version="megabrain-0.1.2:main-auto",
                        commit=False)
                reported_in = usage.get("prompt_tokens")
                reported_out = usage.get("completion_tokens")
                tokens_in = int(reported_in) if isinstance(reported_in, (int, float)) and reported_in > 0 else None
                tokens_out = int(reported_out) if isinstance(reported_out, (int, float)) and reported_out > 0 else None
                cost = usage.get("cost") or usage.get("total_cost")
                self.repository.record(
                    project, events, digest, "success", commit=False,
                    model_selected=usage.get("model_selected"), provider=usage.get("provider"),
                    tokens_in=tokens_in, tokens_out=tokens_out,
                    tokens_in_status="REPORTED" if tokens_in is not None else "ESTIMATED",
                    tokens_out_status="REPORTED" if tokens_out is not None else "UNKNOWN",
                    tokens_in_estimated=None if tokens_in is not None else estimate_tokens(events),
                    tokens_out_estimated=None,
                    estimated_cost=float(cost) if isinstance(cost, (int, float)) and cost > 0 else None,
                    cost_status="REPORTED" if isinstance(cost, (int, float)) and cost > 0 else "UNKNOWN",
                    derived_items_count=len(items),
                    escalation_stage=int(usage.get("escalation_stage") or 1),
                    escalation_reason=usage.get("escalation_reason"))
                self.repository.success(project, events, commit=False)
            self.repository.pg.conn.commit()
            self.heartbeat.update(state="RUNNING", success=True, processed_items=len(events))
            return {"status": "success", "llm_calls": usage.get("escalation_stage", 1), "batch_id": batch,
                    "events": len(events), "items": len(items), "stage": stage_hint, **usage}
        except Exception as error:  # watermark intentionally remains unchanged; batch repeatable
            try:
                self.repository.pg.conn.rollback()
            except Exception:
                pass
            self.repository.failure(project, error, 0)
            self.repository.record(project, events, digest, "failed", error_class=type(error).__name__)
            self.heartbeat.error(error, detail=stage_hint)
            return {"status": "failed", "llm_calls": 1, "error": type(error).__name__, "stage": stage_hint}

    @staticmethod
    def has_important(project: str, events: list[dict]) -> bool:
        return any(importance_signal(_text(event)) >= 2 for event in events)
