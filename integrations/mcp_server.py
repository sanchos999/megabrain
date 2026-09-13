#!/usr/bin/env python3
"""MegaBrain MCP stdio server (JSON-RPC 2.0 over stdio).

Exposes MegaBrain long-term memory to MCP clients (OpenClaw etc.):
  - memory_search(query, depth?)   -> /v1/memory/search
  - memory_remember(text, importance?) -> /v1/events

Stdlib only. Env:
  MB_BASE_URL     default http://127.0.0.1:4300
  MB_API_TOKEN    bearer token (required)
  MB_AGENT        agent label for events (default "mcp")
"""
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

BASE = os.environ.get("MB_BASE_URL", "http://127.0.0.1:4300").rstrip("/")
TOKEN = os.environ.get("MB_API_TOKEN", "")
AGENT = os.environ.get("MB_AGENT", "mcp")


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def tool_search(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query required"}
    depth = args.get("depth") or "WARM"
    body = {"query": q, "depth": depth, "top_k": int(args.get("top_k") or 5)}
    try:
        r = _post("/v1/memory/search", body)
    except Exception as e:  # noqa: BLE001
        return {"error": f"search failed: {e}"}
    items = r.get("results") or r.get("items") or []
    out = []
    for it in items:
        out.append({
            "text": (it.get("text") or "")[:1000],
            "score": it.get("score"),
            "created_at": it.get("created_at"),
        })
    return {"results": out, "mode": r.get("mode"), "depth": depth}


def tool_remember(args: dict) -> dict:
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text required"}
    body = {
        "event_type": "MEMORY",
        "agent": AGENT,
        "payload": {"text": text},
        "importance": args.get("importance"),
        "event_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    body = {k: v for k, v in body.items() if v is not None}
    try:
        r = _post("/v1/events", body)
    except Exception as e:  # noqa: BLE001
        return {"error": f"remember failed: {e}"}
    return {"stored": True, "event_id": r.get("event_id") or body["event_id"]}


TOOLS = {
    "memory_search": {
        "description": "Semantic long-term memory search (MegaBrain). Use to recall facts/preferences/history.",
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
    "memory_remember": {
        "description": "Store a durable fact/episode into long-term memory (MegaBrain).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["text"],
        },
    },
}

HANDLERS = {"memory_search": tool_search, "memory_remember": tool_remember}


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
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
                "serverInfo": {"name": "megabrain", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(rid, {"tools": [
                {"name": n, **spec} for n, spec in TOOLS.items()
            ]})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments") or {}
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
