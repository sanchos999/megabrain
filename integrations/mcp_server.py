#!/usr/bin/env python3
"""MegaBrain MCP stdio server for OpenClaw, scoped to its agent project.

Tools:
  memory_search(query, depth?, top_k?) -> project-scoped /v1/memory/search
  memory_remember(text, kind?, item_key?, status?, importance?) -> /v1/events

For DECISION, CONSTRAINT, and TASK_UPDATE, MegaBrain creates explicit
memory_items. Generic MEMORY records remain source events and are not
misclassified as decisions or constraints.
"""
import json
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import urllib.request
import uuid
from datetime import UTC, datetime

BASE = os.environ.get("MB_BASE_URL", "http://127.0.0.1:4300").rstrip("/")
TOKEN = os.environ.get("MB_API_TOKEN", "")
SOURCE = os.environ.get("MB_SOURCE", "openclaw")
AGENT = os.environ.get("MB_AGENT", "openclaw")
PROFILE = os.environ.get("MB_PROFILE", "main")
AGENT_ID = os.environ.get("MB_AGENT_ID", PROFILE)
CHANNEL = os.environ.get("MB_CHANNEL", "mcp")
PROJECT_ID = os.environ.get("MB_PROJECT_ID", "").strip() or None
LEGACY_PROJECT_ID = os.environ.get("MB_LEGACY_PROJECT_ID", "").strip() or None
SHARED_PROJECTS = {}
for _entry in os.environ.get("MB_SHARED_PROJECTS", "").split(","):
    _label, _sep, _project = _entry.strip().partition("=")
    if _sep and _label and _project:
        SHARED_PROJECTS[_label.strip()] = _project.strip()
STRUCTURED_KINDS = {"DECISION", "CONSTRAINT", "TASK_UPDATE"}
ALL_KINDS = STRUCTURED_KINDS | {"MEMORY"}


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _project_id() -> str | None:
    global PROJECT_ID
    if PROJECT_ID:
        return PROJECT_ID
    result = _post("/v1/resolve-project", {
        "source": SOURCE,
        "profile": PROFILE,
        "channel": CHANNEL,
        "query": "продолжаем",
    })
    PROJECT_ID = result.get("project_id")
    return PROJECT_ID


def tool_search(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query required"}
    depth = str(args.get("depth") or "WARM").upper()
    if depth not in {"HOT", "WARM", "DEEP"}:
        return {"error": "depth must be HOT, WARM, or DEEP"}
    try:
        project_id = _project_id()
        if not project_id:
            return {"error": "project not resolved for OpenClaw agent"}
        limit = max(1, min(20, int(args.get("top_k") or 5)))
        body = {"query": q, "mode": depth, "limit": limit, "project_id": project_id}
        legacy_results, legacy_error = [], None
        legacy_id = LEGACY_PROJECT_ID
        if depth in {"WARM", "DEEP"} and legacy_id and legacy_id != project_id:
            legacy_body = {"query": q, "mode": depth, "limit": limit, "project_id": legacy_id}
            with ThreadPoolExecutor(max_workers=2) as pool:
                own_future = pool.submit(_post, "/v1/memory/search", body)
                legacy_future = pool.submit(_post, "/v1/memory/search", legacy_body)
                r = own_future.result()
                try:
                    legacy = legacy_future.result()
                    legacy_items = legacy.get("results") or legacy.get("items") or []
                    legacy_results = [{**item, "source": "legacy-unassigned"} for item in legacy_items]
                except Exception as e:  # noqa: BLE001
                    legacy_error = type(e).__name__
            search_scope = "agent+legacy-unassigned"
        else:
            r = _post("/v1/memory/search", body)
            search_scope = "agent"
    except Exception as e:  # noqa: BLE001
        return {"error": f"search failed: {e}"}
    if depth == "HOT":
        return {**r, "legacy_results": [], "project_id": project_id,
                "search_scope": "agent"}
    items = r.get("results") or r.get("items") or []
    out = [{
        "text": (it.get("text") or "")[:1000],
        "score": it.get("score"),
        "created_at": it.get("created_at"),
        "source": "agent",
    } for it in items]
    legacy_out = [{
        "text": (it.get("text") or "")[:1000],
        "score": it.get("score"),
        "created_at": it.get("created_at"),
        "source": "legacy-unassigned",
    } for it in legacy_results]
    return {"results": out, "legacy_results": legacy_out, "mode": r.get("mode"),
            "project_id": project_id, "legacy_project_id": legacy_id if search_scope != "agent" else None,
            "search_scope": search_scope, "legacy_search_error": legacy_error}


def tool_search_shared(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query required"}
    if not SHARED_PROJECTS:
        return {"error": "shared project allowlist not configured"}
    requested = args.get("agents")
    if requested is None:
        selected = [name for name in SHARED_PROJECTS if name != AGENT_ID]
    elif not isinstance(requested, list) or any(not isinstance(x, str) for x in requested):
        return {"error": "agents must be an array of allowlisted names"}
    else:
        selected = list(dict.fromkeys(requested))
    unknown = [name for name in selected if name not in SHARED_PROJECTS]
    if unknown:
        return {"error": "agent outside allowlist", "agents": unknown}
    project_map = dict(SHARED_PROJECTS)
    if args.get("include_legacy", True):
        if not LEGACY_PROJECT_ID:
            return {"error": "legacy archive is not configured"}
        project_map["legacy-unassigned"] = LEGACY_PROJECT_ID
        if "legacy-unassigned" not in selected:
            selected.append("legacy-unassigned")
    depth = str(args.get("depth") or "WARM").upper()
    if depth not in {"WARM", "DEEP"}:
        depth = "WARM"
    try:
        per_agent = max(1, min(5, int(args.get("top_k") or 3)))
    except (TypeError, ValueError):
        return {"error": "top_k must be an integer from 1 to 5"}
    results, errors = [], []
    if selected:
        with ThreadPoolExecutor(max_workers=min(8, len(selected))) as pool:
            futures = {
                label: pool.submit(_post, "/v1/memory/search", {
                    "query": q, "mode": depth, "limit": per_agent,
                    "project_id": project_map[label],
                })
                for label in selected
            }
            for label in selected:
                try:
                    response = futures[label].result()
                    for item in response.get("results") or []:
                        results.append({
                            "agent": label, "project_id": project_map[label],
                            "text": (item.get("text") or "")[:800],
                            "score": item.get("score"),
                            "created_at": item.get("created_at"),
                            "event_type": item.get("event_type"),
                        })
                except Exception as error:
                    errors.append({"agent": label, "error": type(error).__name__})
    results.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return {"scope": "shared", "depth": depth, "queried_agents": selected,
            "results": results[:20], "errors": errors, "ok": not errors}


def tool_remember(args: dict) -> dict:
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text required"}
    kind = str(args.get("kind") or "MEMORY").upper()
    if kind not in ALL_KINDS:
        return {"error": "kind must be MEMORY, DECISION, CONSTRAINT, or TASK_UPDATE"}
    try:
        project_id = _project_id()
        if not project_id:
            return {"error": "project not resolved for OpenClaw agent"}
        payload = {"text": text}
        if args.get("item_key"):
            payload["item_key"] = str(args["item_key"]).strip()
        if args.get("status"):
            payload["status"] = str(args["status"]).upper()
        importance = args.get("importance")
        if importance is not None:
            importance = float(importance)
            if not 0 <= importance <= 1:
                return {"error": "importance must be between 0 and 1"}
            payload["importance"] = importance
        event_id = str(uuid.uuid4())
        body = {
            "event_id": event_id,
            "source": SOURCE,
            "source_instance": f"{SOURCE}:{AGENT}",
            "agent_id": AGENT_ID,
            "project_id": project_id,
            "event_type": kind,
            "payload": payload,
            "metadata": {
                "agent": AGENT_ID,
                "profile": PROFILE,
                "channel": CHANNEL,
                "importance": importance,
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
        r = _post("/v1/events", body)
    except Exception as e:  # noqa: BLE001
        return {"error": f"remember failed: {e}"}
    return {
        "stored": True,
        "event_id": r.get("event_id") or event_id,
        "project_id": project_id,
        "kind": kind,
        "structured": kind in STRUCTURED_KINDS,
        "agent_id": AGENT_ID,
    }


TOOLS = {
    "memory_search": {
        "description": "Search this OpenClaw agent's MegaBrain project and, for WARM/DEEP, the unassigned legacy archive in separate result groups. Legacy records are owner-unknown; use memory_search_shared when results are insufficient.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "depth": {"type": "string", "enum": ["HOT", "WARM", "DEEP"]},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    "memory_search_shared": {
        "description": (
            "Search other agents' MegaBrain projects when a cross-agent search is needed or this agent's own search is insufficient. "
            "Results identify their source. The shared unassigned legacy archive is included by default and marks records as owner unknown; set include_legacy=false to omit it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "agents": {"type": "array", "items": {"type": "string"}},
                "depth": {"type": "string", "enum": ["WARM", "DEEP"]},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
                "include_legacy": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    "memory_remember": {
        "description": (
            "Store memory in this OpenClaw agent's MegaBrain project. "
            "Choose DECISION for an explicit decision, CONSTRAINT for a durable "
            "requirement or preference, TASK_UPDATE for a task status, and MEMORY "
            "only for a general fact or episode. Do not misclassify: only the first "
            "three kinds create structured memory_items. Provide item_key when "
            "later updates should replace the same item."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": ["MEMORY", "DECISION", "CONSTRAINT", "TASK_UPDATE"]},
                "item_key": {"type": "string"},
                "status": {"type": "string"},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["text"],
        },
    },
}

HANDLERS = {"memory_search": tool_search, "memory_search_shared": tool_search_shared,
            "memory_remember": tool_remember}


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    if not TOKEN:
        sys.stderr.write("MB_API_TOKEN not set\n")
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            reply(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "megabrain", "version": "1.1.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(rid, {"tools": [{"name": n, **spec} for n, spec in TOOLS.items()]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = HANDLERS.get(name)
            if not fn:
                reply(rid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
            else:
                out = fn(args)
                reply(rid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]})
        elif method == "ping":
            reply(rid, {})


if __name__ == "__main__":
    main()
