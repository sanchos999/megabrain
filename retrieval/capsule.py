"""Context Capsule builder — the main read API result.

Sections: PROJECT, CURRENT_STATE, CONFIRMED_DECISIONS, CONSTRAINTS, OPEN_WORK,
RECENT_CHANGES, KNOWN_FAILURES, IMPORTANT_FACTS, RELEVANT_EXPERIENCE, SOURCES.

Built WITHOUT LLM. Token budget respected by priority:
constraints > current_state > open_work > decisions > recent_changes >
known_failures > important_facts. Whole-structure inclusion (no mid-text cuts).

Stable vs delta split for future prompt caching:
STABLE = decisions/constraints/project state (slow-changing)
DELTA  = recent changes (last events since capsule_revision boundary).
"""
from __future__ import annotations

import time

SECTION_PRIORITY = [
    "constraints", "current_state", "open_work", "confirmed_decisions",
    "recent_changes", "known_failures", "important_facts", "relevant_experience",
]


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    if not text:
        return 0
    return max(1, len(text) // chars_per_token)


def _item_to_text(item: dict) -> str:
    c = item.get("content") or {}
    text = c.get("text") or c.get("summary") or c.get("title") or ""
    key = c.get("item_key")
    status = c.get("status")
    parts = []
    if key:
        parts.append(key)
    if text:
        parts.append(text)
    if status:
        parts.append(f"[{status}]")
    return " ".join(parts)


class CapsuleBuilder:
    def __init__(self, pg, ram, redis_layer, telemetry, cfg):
        self.pg = pg
        self.ram = ram
        self.redis = redis_layer
        self.telemetry = telemetry
        self.cfg = cfg

    def _hot_state(self, project_id: str) -> dict | None:
        """L0 RAM first, then Redis, then PG (fills caches)."""
        st = self.ram.get(project_id)
        if st is not None:
            self.telemetry.inc("ram_hit")
            return st
        st = self.redis.get_json(f"project:{project_id}:hot")
        if st is not None:
            self.telemetry.inc("redis_hot_hit")
            self.ram.put(project_id, st)
            return st
        # rebuild from PG
        items = self.pg.current_memory(project_id)
        proj = self.pg.get_project(project_id)
        st = {
            "project": proj,
            "items": items,
            "revision": proj["revision"] if proj else None,
        }
        self.ram.put(project_id, st)
        self.redis.set_json(f"project:{project_id}:hot", st, ttl=3600)
        return st

    def build(self, project_id: str, token_budget: int | None = None,
              since_revision: int | None = None) -> dict:
        t0 = time.perf_counter()
        budget = token_budget or self.cfg.get("capsule_default_token_budget", 2000)
        cpt = self.cfg.get("token_estimate_chars_per_token", 4)

        hot = self._hot_state(project_id)
        proj = hot.get("project") or {}
        items = hot.get("items") or []

        decisions = [i for i in items if i["kind"] == "DECISION"]
        constraints = [i for i in items if i["kind"] == "CONSTRAINT"]
        tasks = [i for i in items if i["kind"] == "TASK"]
        experiences = [i for i in items if i["kind"] in
                       ("EXPERIENCE", "PROCEDURE", "REJECTED_APPROACH")]
        failure_patterns = [i for i in items if i["kind"] == "FAILURE_PATTERN"]
        facts = [i for i in items if i["kind"] == "FACT"]

        # recent significant events (exclude turn bookkeeping)
        sig_types = ["DECISION", "CONSTRAINT", "TASK_UPDATE", "ERROR", "TEST_RESULT",
                     "FILE_WRITE", "ASSISTANT_MESSAGE"]
        recent = self.pg.recent_events(project_id, limit=self.cfg.get("max_recent_events", 20),
                                       types=sig_types)
        recent_changes = [
            {"event_id": r["event_id"], "event_type": r["event_type"],
             "created_at": r["created_at"], "payload": r["payload"]}
            for r in reversed(recent)
        ]
        failures = self.pg.recent_events(project_id, limit=10, types=["ERROR"])

        # delta: only events after since_revision boundary (project revision increments per event)
        delta_changes = recent_changes
        if since_revision is not None:
            # events are appended in order; use project revision numbering as proxy
            proj_rev = proj.get("revision") or 0
            if proj_rev <= since_revision:
                delta_changes = []

        section_payloads = {
            "constraints": constraints,
            "current_state": [{
                "status": proj.get("status"),
                "revision": proj.get("revision"),
                "updated_at": str(proj.get("updated_at")),
            }],
            "open_work": [t for t in tasks if (t["content"].get("status") or "OPEN") == "OPEN"],
            "confirmed_decisions": [d for d in decisions if d["valid_to"] is None],
            "recent_changes": delta_changes,
            "known_failures": [
                {"event_id": f["event_id"], "created_at": f["created_at"],
                 "payload": f["payload"]} for f in failures
            ] + [
                {"item_id": fp["item_id"], "kind": "FAILURE_PATTERN",
                 "content": fp["content"], "confidence": fp.get("confidence"),
                 "source_event_ids": fp.get("source_event_ids")}
                for fp in failure_patterns if fp["valid_to"] is None
            ],
            "important_facts": [
                {"item_id": f["item_id"], "content": f["content"],
                 "source_event_ids": f.get("source_event_ids")}
                for f in facts if f["valid_to"] is None
            ],
            "relevant_experience": [
                {"item_id": e["item_id"], "kind": e["kind"],
                 "content": e["content"], "confidence": e.get("confidence"),
                 "source_event_ids": e.get("source_event_ids")}
                for e in experiences if e["valid_to"] is None
            ],
        }

        # budget packing: whole sections in priority order
        included, omitted = [], []
        used_tokens = 0
        for sec in SECTION_PRIORITY:
            payload = section_payloads[sec]
            if not payload:
                continue
            stext = self._render(sec, payload)
            stoks = estimate_tokens(stext, cpt)
            if used_tokens + stoks <= budget:
                included.append(sec)
                used_tokens += stoks
            else:
                # try to include partial list at item granularity (whole items only)
                if isinstance(payload, list):
                    part, ptoks = [], 0
                    for item in payload:
                        it = self._render(sec, [item])
                        itk = estimate_tokens(it, cpt)
                        if used_tokens + ptoks + itk > budget:
                            break
                        part.append(item)
                        ptoks += itk
                    if part:
                        included.append(sec)
                        used_tokens += ptoks
                        section_payloads[sec] = part
                    else:
                        omitted.append(sec)
                else:
                    omitted.append(sec)

        source_event_ids = sorted({
            eid for item in items if item["valid_to"] is None
            for eid in (item.get("source_event_ids") or [])
        } | {r["event_id"] for r in recent_changes})

        capsule = {
            "capsule_revision": f"{project_id}:{proj.get('revision', 0)}",
            "project": {
                "project_id": project_id,
                "name": proj.get("name"),
                "status": proj.get("status"),
                "revision": proj.get("revision"),
            },
            "sections": {
                "PROJECT": {"project_id": project_id, "name": proj.get("name"),
                            "status": proj.get("status")},
                "CURRENT_STATE": section_payloads["current_state"] if "current_state" in included else [],
                "CONFIRMED_DECISIONS": section_payloads["confirmed_decisions"] if "confirmed_decisions" in included else [],
                "CONSTRAINTS": section_payloads["constraints"] if "constraints" in included else [],
                "OPEN_WORK": section_payloads["open_work"] if "open_work" in included else [],
                "RECENT_CHANGES": section_payloads["recent_changes"] if "recent_changes" in included else [],
                "KNOWN_FAILURES": section_payloads["known_failures"] if "known_failures" in included else [],
                "IMPORTANT_FACTS": section_payloads["important_facts"] if "important_facts" in included else [],
                "RELEVANT_EXPERIENCE": section_payloads["relevant_experience"] if "relevant_experience" in included else [],
            },
            "stable": self._stable_view(project_id, section_payloads, included),
            "delta": {"recent_changes": section_payloads.get("recent_changes", []),
                      "since_revision": since_revision},
            "sources": source_event_ids,
            "metadata": {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "token_estimate": used_tokens,
                "token_budget": budget,
                "included": included,
                "omitted": omitted,
                "llm_used": False,
            },
        }
        dt = (time.perf_counter() - t0) * 1000
        self.telemetry.observe("context_build_ms", dt)
        self.telemetry.inc("capsule_built")
        self.telemetry.observe("capsule_tokens", used_tokens)
        return capsule

    def _stable_view(self, project_id, payloads, included) -> dict:
        """Slow-changing part for future prompt caching."""
        return {
            "project_id": project_id,
            "current_state": payloads["current_state"] if "current_state" in included else [],
            "constraints": payloads["constraints"] if "constraints" in included else [],
            "confirmed_decisions": payloads["confirmed_decisions"] if "confirmed_decisions" in included else [],
            "open_work": payloads["open_work"] if "open_work" in included else [],
        }

    def _render(self, sec: str, payload) -> str:
        if isinstance(payload, list):
            return "\n".join(_item_to_text(i) if "content" in i else str(i) for i in payload)
        return str(payload)
