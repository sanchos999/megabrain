"""Project resolver: fast, deterministic, no LLM/vector/graph.

Priority:
1. explicit project_id
2. active session mapping (RAM/Redis -> PG)
3. recent ACTIVE project by updated_at
4. deterministic signals in text (project_id or project name mention)
M1: no semantic fallback.
"""
from __future__ import annotations

import time

from core.hot import L0RAM, L1Redis

CONTINUATION_PHRASES = {
    "продолжаем", "продолжи", "продолжить", "делай дальше", "что осталось",
    "на чём остановились", "на чем остановились", "go on", "continue",
    "what's left", "продолжение"
}


def is_continuation_query(text: str) -> bool:
    t = (text or "").strip().lower().rstrip("?!.")
    return t in CONTINUATION_PHRASES


class ProjectResolver:
    def __init__(self, pg, ram: L0RAM, redis_layer: L1Redis, telemetry):
        self.pg = pg
        self.ram = ram
        self.redis = redis_layer
        self.telemetry = telemetry

    def resolve(self, *, project_id: str | None = None,
                session_id: str | None = None, query: str | None = None,
                source: str = "hermes", profile: str = "default", channel: str = "cli",
                parent_session_id: str | None = None, conversation_id: str | None = None) -> dict:
        t0 = time.perf_counter()
        def result(pid, reason, confidence=1.0, source_name=None):
            self._finish(t0, reason)
            return {"project_id": pid, "resolved": bool(pid), "reason": reason,
                    "mode": reason, "confidence": confidence, "source": source_name or reason.lower()}

        if project_id and self.pg.get_project(project_id):
            if session_id:
                self.pg.bind_project_context(session_id=session_id, project_id=project_id,
                                             source=source, profile=profile, channel=channel,
                                             parent_session_id=parent_session_id,
                                             conversation_id=conversation_id, reason="EXPLICIT")
            else:
                self.pg.set_profile_project(source=source, profile=profile, channel=channel,
                                            project_id=project_id, reason="EXPLICIT")
            return result(project_id, "EXPLICIT", source_name="explicit")

        if session_id:
            pid = self.redis.get_str(f"session:{session_id}") or self.pg.get_session_project(session_id)
            if pid:
                return result(pid, "SESSION_MAPPING_PG", source_name="session")

        if parent_session_id:
            pid = self.pg.get_session_project(parent_session_id)
            if pid:
                self.pg.bind_project_context(session_id=session_id or parent_session_id, project_id=pid,
                                             source=source, profile=profile, channel=channel,
                                             parent_session_id=parent_session_id, conversation_id=conversation_id,
                                             reason="RESUME_INHERIT")
                return result(pid, "RESUME_INHERIT", source_name="parent_session")

        if conversation_id:
            # Conversation mappings are represented by the scoped active map until a
            # dedicated conversation table is needed; no global fallback is used.
            pid = self.pg.get_profile_project(source=source, profile=profile, channel=channel)
            if pid:
                return result(pid, "CONVERSATION_MAPPING", source_name="conversation")

        if query:
            q = query.lower()
            matches = [p for p in self.pg.list_projects()
                       if p["project_id"].lower() in q or (p["name"] and p["name"].lower() in q)]
            if len(matches) == 1:
                return result(matches[0]["project_id"], "DETERMINISTIC", source_name="text_match")
            if len(matches) > 1:
                return result(None, "AMBIGUOUS", confidence=0.0, source_name="ambiguous")

        if query and is_continuation_query(query):
            pid = self.pg.get_profile_project(source=source, profile=profile, channel=channel)
            if pid:
                return result(pid, "PROFILE_ACTIVE", source_name="profile")

        return result(None, "UNRESOLVED", confidence=0.0, source_name=None)

    def _finish(self, t0, mode: str):
        dt = (time.perf_counter() - t0) * 1000
        self.telemetry.observe("project_resolve_ms", dt)
        self.telemetry.inc(f"resolver_mode_{mode}")
