"""Structured memory type schemas (M1: DECISION/CONSTRAINT/TASK/PROJECT_STATE
implemented; остальные schema-only до M3-M5).

Общие temporal+provenance поля каждого derived memory item:
  valid_from, valid_to, supersedes_id, confidence,
  source_event_ids, extractor_type (EXPLICIT|DETERMINISTIC|MANUAL|LLM),
  extractor_version, created_at.
"""

DECISION = {
    "item_key": "str (upsert key)",
    "text": "str",
    "status": "optional: OPEN|REVOKED",
    "confidence": "optional float 0..1",
}

CONSTRAINT = {
    "item_key": "str",
    "text": "str",
    "status": "optional: OPEN|REVOKED",
}

TASK = {
    "item_key": "str",
    "text": "str",
    "status": "OPEN|DONE|CANCELLED",
}

PROJECT_STATE = {
    "revision": "int (monotonic per project)",
    "status": "ACTIVE|PAUSED|COMPLETED|ARCHIVED",
}

# ---- schema-only (M3-M5) ----
EPISODE = {"title": "str", "summary": "str", "time": "timestamptz", "participants": "list"}
FACT = {"subject": "str", "predicate": "str", "object": "str", "polarity": "+|-"}
ENTITY = {"name": "str", "type": "str"}
RELATION = {"subject": "str", "predicate": "str", "object": "str"}
PROCEDURE = {"name": "str", "steps": "list"}
FAILURE_PATTERN = {"pattern": "str", "context": "str", "resolution": "str"}
EXPERIENCE = {"insight": "str", "generalization": "str"}
REJECTED_APPROACH = {"approach": "str", "reason": "str"}
