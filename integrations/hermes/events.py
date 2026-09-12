"""Event builders for the Hermes -> MegaBrain capture path.

Every event has: source='hermes', event_type, created_at (ISO, local tz),
session_id, and an idempotent event_id (deterministic, so re-emission never
duplicates). Payload carries the meaningful content; large bodies go through
MegaBrain's blob policy server-side.

Event types captured (M5 Section 5):
  USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT, SHELL_COMMAND,
  SHELL_RESULT, FILE_READ, FILE_WRITE, FILE_DIFF, TEST_STARTED, TEST_RESULT,
  ERROR, DECISION, CONSTRAINT, TURN_STARTED, TURN_COMPLETED, TURN_ABORTED.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

SOURCE = "hermes"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _event_id(event_type: str, session_id: str, *parts: str) -> str:
    raw = "|".join([event_type, session_id, *parts])
    return "mb_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def base(event_type: str, session_id: str, *, payload: dict | None = None,
         project_id: str | None = None, turn_id: str | None = None,
         channel: str = "cli", event_id: str | None = None,
         parent_event_id: str | None = None) -> dict:
    ev = {
        "source": SOURCE,
        "source_instance": channel,
        "session_id": session_id,
        "event_type": event_type,
        "created_at": _now(),
        "metadata": {"channel": channel},
    }
    if event_id:
        ev["event_id"] = event_id
    if project_id:
        ev["project_id"] = project_id
    if turn_id:
        ev["turn_id"] = turn_id
    if parent_event_id:
        ev["parent_event_id"] = parent_event_id
    if payload is not None:
        ev["payload"] = payload
    return ev


def user_message(session_id: str, text: str, *, project_id=None, turn_id=None,
                 channel="cli") -> dict:
    eid = _event_id("USER_MESSAGE", session_id, text[:200])
    return base("USER_MESSAGE", session_id, payload={"text": text},
                project_id=project_id, turn_id=turn_id, channel=channel,
                event_id=eid)


def assistant_message(session_id: str, text: str, *, project_id=None,
                      turn_id=None, channel="cli", model=None,
                      route=None) -> dict:
    eid = _event_id("ASSISTANT_MESSAGE", session_id, text[:200])
    return base("ASSISTANT_MESSAGE", session_id, payload={"text": text},
                project_id=project_id, turn_id=turn_id, channel=channel,
                event_id=eid)


def tool_call(session_id: str, tool_name: str, args: dict, *, project_id=None,
              turn_id=None, channel="cli") -> dict:
    eid = _event_id("TOOL_CALL", session_id, tool_name, str(args)[:200])
    return base("TOOL_CALL", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"tool": tool_name, "args": args})


def tool_result(session_id: str, tool_name: str, result: str, *, project_id=None,
                turn_id=None, channel="cli", is_error=False) -> dict:
    eid = _event_id("TOOL_RESULT", session_id, tool_name, result[:200])
    return base("TOOL_RESULT", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"tool": tool_name, "result": result,
                         "is_error": is_error})


def shell_command(session_id: str, command: str, *, project_id=None,
                  turn_id=None, channel="cli") -> dict:
    eid = _event_id("SHELL_COMMAND", session_id, command[:300])
    return base("SHELL_COMMAND", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"command": command})


def shell_result(session_id: str, command: str, output: str, exit_code: int,
                 *, project_id=None, turn_id=None, channel="cli") -> dict:
    eid = _event_id("SHELL_RESULT", session_id, command[:300], str(exit_code))
    return base("SHELL_RESULT", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"command": command, "output": output,
                         "exit_code": exit_code})


def file_write(session_id: str, path: str, *, project_id=None, turn_id=None,
               channel="cli") -> dict:
    eid = _event_id("FILE_WRITE", session_id, path)
    return base("FILE_WRITE", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid, payload={"path": path})


def file_read(session_id: str, path: str, *, project_id=None, turn_id=None,
              channel="cli") -> dict:
    eid = _event_id("FILE_READ", session_id, path)
    return base("FILE_READ", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid, payload={"path": path})


def error(session_id: str, message: str, *, project_id=None, turn_id=None,
          channel="cli", context=None) -> dict:
    eid = _event_id("ERROR", session_id, message[:200])
    return base("ERROR", session_id, turn_id=turn_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"message": message, "context": context})


def turn_started(session_id: str, turn_number: int, message: str, *,
                 project_id=None, channel="cli") -> dict:
    eid = _event_id("TURN_STARTED", session_id, str(turn_number))
    return base("TURN_STARTED", session_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"turn_number": turn_number, "message": message})


def turn_completed(session_id: str, turn_number: int, *, project_id=None,
                   channel="cli") -> dict:
    eid = _event_id("TURN_COMPLETED", session_id, str(turn_number))
    return base("TURN_COMPLETED", session_id, channel=channel,
                project_id=project_id, event_id=eid,
                payload={"turn_number": turn_number})
