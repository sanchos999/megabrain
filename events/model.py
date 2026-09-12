"""Event model: immutable raw event schema + validation.

Event types: USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT,
SHELL_COMMAND, SHELL_RESULT, FILE_READ, FILE_WRITE, FILE_DIFF,
TEST_STARTED, TEST_RESULT, ERROR, DECISION, CONSTRAINT, TASK_UPDATE,
TURN_STARTED, TURN_COMPLETED, TURN_ABORTED.

Structured types DECISION/CONSTRAINT/TASK_UPDATE carry explicit structured
content in payload (no LLM extraction — EXPLICIT extractor only).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

EVENT_TYPES = {
    "USER_MESSAGE", "ASSISTANT_MESSAGE",
    "TOOL_CALL", "TOOL_RESULT",
    "SHELL_COMMAND", "SHELL_RESULT",
    "FILE_READ", "FILE_WRITE", "FILE_DIFF",
    "TEST_STARTED", "TEST_RESULT",
    "ERROR",
    "DECISION", "CONSTRAINT", "TASK_UPDATE",
    "TURN_STARTED", "TURN_COMPLETED", "TURN_ABORTED",
    "IMPORTED_RECORD",          # M2: unclassifiable imported source record (lossless fallback)
}

SCHEMA_VERSION = 1


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%06dZ" % (int(time.time() * 1e6) % 1_000_000)


def validate_event(ev: dict) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []
    et = ev.get("event_type")
    if et not in EVENT_TYPES:
        errors.append(f"event_type '{et}' not in known set")
    if not ev.get("source"):
        errors.append("source is required")
    if not ev.get("created_at"):
        errors.append("created_at is required")
    if ev.get("schema_version") is None:
        ev.setdefault("schema_version", SCHEMA_VERSION)
    if not ev.get("event_id"):
        ev["event_id"] = "evt_" + uuid.uuid4().hex
    return errors


def normalize_event(ev: dict) -> dict:
    """Fill defaults; compute payload_hash if absent. Returns new dict."""
    out = dict(ev)
    if "event_id" not in out or not out["event_id"]:
        out["event_id"] = "evt_" + uuid.uuid4().hex
    out.setdefault("schema_version", SCHEMA_VERSION)
    out.setdefault("source_instance", None)
    out.setdefault("request_id", None)
    out.setdefault("turn_id", None)
    out.setdefault("sequence", None)
    out.setdefault("session_id", None)
    out.setdefault("project_id", None)
    out.setdefault("observed_at", None)
    if not out["observed_at"]:
        out["observed_at"] = now_iso()
    out.setdefault("payload", None)
    out.setdefault("model", None)
    out.setdefault("route", None)
    out.setdefault("provider", None)
    out.setdefault("parent_event_id", None)
    out.setdefault("correlation_id", None)
    out.setdefault("metadata", {})
    if "payload_hash" not in out or not out["payload_hash"]:
        out["payload_hash"] = sha256_hex(canonical_json(out["payload"])) if out["payload"] is not None else None
    return out
