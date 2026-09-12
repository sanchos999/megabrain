"""MegaBrain memory provider plugin for Hermes Agent (M5).

Integrates Hermes with the MegaBrain memory service over REST only. No imports
from MegaBrain internals; no Model Router changes; no Hermes core patch.

Capture: sync_turn / on_turn_start / on_session_end -> local durable SQLite
outbox -> async sender -> MegaBrain /v1/events (non-blocking).

Recall: prefetch -> deterministic mode select (NONE/HOT/WARM/DEEP, no LLM) ->
/v1/resolve-project -> /v1/memory/context (+ /v1/memory/search for WARM/DEEP).
Stable capsule cached by (project_id, project_revision, token_budget) so the
injected prefix stays byte-stable across turns (prompt-cache safe).

Tools: memory_status / memory_project / memory_search / memory_context / memory_off.

Activation: config.yaml -> memory.provider: megabrain
Config: hermes memory setup  (env: MB_BASE_URL, MB_API_TOKEN)
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


def _trace(*_args, **_kwargs) -> None:
    return

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

try:
    from . import mb_events as E
    from .megabrain_client import (
        MegaBrainClient,
        MegaBrainUnavailable,
    )
    from .outbox import Outbox
    from .sender import Sender
except ImportError:  # flat vendored plugin loaded by Hermes
    import mb_events as E
    from megabrain_client import (
        MegaBrainClient,
        MegaBrainUnavailable,
    )
    from outbox import Outbox
    from sender import Sender

try:
    from agent.memory_provider import (
        MemoryProvider,
        RecallStatus,
        is_trivial_prompt,
    )
except ModuleNotFoundError:
    class MemoryProvider:
        pass
    class RecallStatus:
        pass
    def is_trivial_prompt(value):
        return False


# --- deterministic mode selector (M5 section 8, no LLM) --------------------

_CONTINUATION = ("продолжаем", "продолжи", "дальше", "что осталось", "на чём остановились")
_DEEP = ("за всю историю", "в других проектах", "что делали раньше", "как это связано",
         "что мы делали", "похожее", "аналогичное")
_TRIVIAL_RU = ("привет", "здравствуй", "ок", "да", "нет", "спасибо", "понял",
               "понятно", "хорошо", "ага", "угу", "готово")


def select_mode(query: str) -> str:
    q = (query or "").strip().lower()
    if not q or is_trivial_prompt(query) or q in _TRIVIAL_RU:
        return "NONE"
    if any(k in q for k in _DEEP):
        return "DEEP"
    if any(k in q for k in _CONTINUATION):
        return "HOT"
    # WARM signals: decision/failure/location recall
    warm = ("решили", "решение", "ошибка", "ошибк", "обсуждали", "где", "как настро",
            "какой", "какие", "что было", "почему")
    if any(k in q for k in warm):
        return "WARM"
    return "HOT"  # default: cheap structured context, no deep search


def classify_memory_intent(query: str) -> str | None:
    q = " ".join((query or "").strip().lower().split())
    if q in {"продолжаем", "продолжим", "дальше", "давай дальше", "что осталось", "где остановились", "на чем остановились", "continue", "continue working"}:
        return "HOT"
    if any(x in q for x in ("что решили", "что мы решили", "какая была ошибка", "где обсуждали", "вспомни решение")):
        return "WARM"
    if any(x in q for x in ("за всю историю", "что делали раньше", "в других сессиях", "в других проектах", "как это связано с прошлой работой")):
        return "DEEP"
    return None


def _resolve_api_token() -> str:
    for name in ("MB_API_TOKEN",):
        value = os.environ.get(name)
        if value:
            return value.strip()
    for env_name in ("MB_API_TOKEN_FILE", "MB_TOKEN_FILE"):
        path = os.environ.get(env_name)
        if path and Path(path).is_file():
            return Path(path).read_text(encoding="utf-8").strip()
    fallback = Path.home() / ".config/megabrain/token"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    return ""


def _channel_from_kwargs(agent_context: str = "", platform: str = "") -> str:
    if agent_context in ("subagent", "cron", "flush"):
        return agent_context
    return platform or "cli"


def _explicit_directive(text: str) -> tuple[str, str, str] | None:
    """Parse only explicit user markers; never infer from prose or assistant text."""
    import re
    raw = (text or "").strip()
    m = re.search(r"(?:РЕШЕНИЕ|DECISION)\s*:\s*(.+?)(?:\.|$)", raw, re.IGNORECASE | re.DOTALL)
    if m:
        return "DECISION", m.group(1).strip(), "decision"
    m = re.search(r"(?:ОГРАНИЧЕНИЕ|CONSTRAINT)\s*:\s*(.+?)(?:\.|$)", raw, re.IGNORECASE | re.DOTALL)
    if m:
        return "CONSTRAINT", m.group(1).strip(), "constraint"
    m = re.search(r"(?:ЗАДАЧА|TASK)\s*:\s*(.+?)(?:\.|$)", raw, re.IGNORECASE | re.DOTALL)
    if m:
        return "TASK_UPDATE", m.group(1).strip(), "task"
    return None


class MegaBrainProvider(MemoryProvider):
    name = "megabrain"

    def __init__(self):
        self._client: MegaBrainClient | None = None
        self._outbox: Outbox | None = None
        self._sender: Sender | None = None
        self._hermes_home: str = ""
        self._platform: str = "cli"
        self._agent_context: str = "primary"
        self._session_id: str = ""
        self._turn_number: int = 0
        self._project_id: str | None = None
        self._cache: dict[str, tuple] = {}  # key -> (revision, capsule_text)
        self._last_recall: RecallStatus | None = None
        self._off = False  # memory_off toggle for current session

    # -- availability / lifecycle ------------------------------------------

    def is_available(self) -> bool:
        # configured? base_url default loopback, token optional (auth may be off)
        return True  # no network here; real check happens in initialize()

    def unavailable_reason(self) -> str:
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        _trace("PLUGIN_INITIALIZE_START", session_id=session_id, status="start")
        self._hermes_home = kwargs.get("hermes_home", "~/.hermes")
        self._hermes_home = os.path.expanduser(self._hermes_home)
        self._platform = kwargs.get("platform", "cli") or "cli"
        self._agent_context = kwargs.get("agent_context", "primary") or "primary"
        self._session_id = session_id
        self._project_state_path = Path(self._hermes_home) / "megabrain-project.json"
        try:
            self._project_id = json.loads(self._project_state_path.read_text()).get("project_id")
        except Exception:
            self._project_id = None
        base_url = os.environ.get("MB_BASE_URL", "http://127.0.0.1:4300")
        token = _resolve_api_token()
        self._client = MegaBrainClient(base_url, token=token, timeout=3.0)
        self._profile = (kwargs.get("profile_name") or kwargs.get("agent_identity") or
                         os.environ.get("HERMES_PROFILE") or
                         Path(self._hermes_home).name or "default")
        self._parent_session_id = kwargs.get("parent_session_id") or kwargs.get("resume_from")
        self._conversation_id = kwargs.get("conversation_id") or kwargs.get("thread_id")
        self._channel = kwargs.get("platform", "cli") or "cli"
        outbox_path = Path(self._hermes_home) / "megabrain-outbox.db"
        self._outbox = Outbox(outbox_path)
        # Delivery is owned by the persistent r7canary systemd sender. The provider only
        # commits events to SQLite, so short-lived oneshot processes cannot lose delivery.
        self._sender = None
        # resolve current project once (session mapping)
        try:
            r = self._client.resolve_project(session_id=session_id,
                                             source="hermes", profile=self._profile,
                                             channel=self._channel,
                                             parent_session_id=self._parent_session_id,
                                             conversation_id=self._conversation_id)
            resolved_id = r.get("project_id")
            if resolved_id:
                self._project_id = resolved_id
            elif not self._project_id and not self._parent_session_id:
                active = self._client.resolve_project(
                    session_id=session_id, query="продолжаем",
                    source="hermes", profile=self._profile,
                    channel=self._channel)
                self._project_id = active.get("project_id")
            if self._project_id:
                self._project_state_path.write_text(json.dumps({"project_id": self._project_id}))
                self._client.resolve_project(
                    project_id=self._project_id, session_id=session_id,
                    source="hermes", profile=self._profile,
                    channel=self._channel)
        except Exception:
            pass
        _trace("PLUGIN_INIT_END", session_id=session_id, project_id=self._project_id, status="ok")

    # -- recall ------------------------------------------------------------

    def _warm(self, query: str, session_id: str) -> None:
        """Background recall: resolve + context, cache the stable capsule."""
        try:
            r = self._client.resolve_project(session_id=session_id, query=query,
                                             source="hermes", profile=self._profile,
                                             channel=self._channel,
                                             parent_session_id=self._parent_session_id,
                                             conversation_id=self._conversation_id)
            pid = r.get("project_id")
            if not pid:
                return
            mode = select_mode(query)
            cap = self._client.get_context(project_id=pid,
                                           token_budget=_budget_for(mode))
            rev = cap.get("revision") or cap.get("project_revision")
            text = _format_capsule(cap, mode)
            if mode in ("WARM", "DEEP"):
                ev = self._client.memory_search(query, project_id=pid, limit=5,
                                                mode=mode)
                text += _format_evidence(ev)
            self._cache[session_id] = (rev, text, pid)
            self._project_id = pid
        except Exception:
            pass  # degraded: no recall, never block

    def should_recall(self, prompt: str) -> bool | None:
        return True if classify_memory_intent(prompt) is not None else None

    def is_memory_intent(self, query: str) -> bool:
        return classify_memory_intent(query) is not None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._off or (is_trivial_prompt(query) and (query or "").strip().lower() not in {"продолжаем", "продолжи", "continue"}):
            self._last_recall = None
            return ""
        sid = session_id or self._session_id
        cached = self._cache.get(sid)
        if cached:
            _, text, _ = cached
            self._last_recall = RecallStatus("megabrain", 1)
            return text
        # cold-start: bounded synchronous fetch (local loopback, LLM-free)
        t = threading.Thread(target=self._warm, args=(query, sid), daemon=True)
        t.start()
        t.join(1.0)
        cached = self._cache.get(sid)
        if cached:
            self._last_recall = RecallStatus("megabrain", 1)
            return cached[1]
        self._last_recall = None
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        sid = session_id or self._session_id
        t = threading.Thread(target=self._warm, args=(query, sid), daemon=True)
        t.start()

    def recall_status(self) -> RecallStatus | None:
        return self._last_recall

    # -- capture -----------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        _trace("ON_TURN_START_START", session_id=self._session_id, status="start")
        if self._agent_context != "primary":
            return
        self._turn_number = turn_number
        self._current_mode = select_mode(message)
        event = E.turn_started(
            self._session_id, turn_number, message, project_id=self._project_id, channel=self._platform)
        self._enqueue(event)
        _trace("ON_TURN_START_END", session_id=self._session_id, status="ok")

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list[dict[str, Any]] | None = None) -> None:
        """Persist a completed turn, non-blocking (outbox append is local+fast)."""
        _trace("SYNC_TURN_START", session_id=session_id or self._session_id, status="start")
        if self._agent_context != "primary":
            return
        sid = session_id or self._session_id
        pid = self._project_id
        # user + assistant (already idempotent via deterministic event_id)
        if user_content:
            user_event = E.user_message(sid, user_content, project_id=pid,
                                        channel=self._platform)
            self._enqueue(user_event)
            directive = _explicit_directive(user_content)
            if directive and pid:
                event_type, text, _ = directive
                self._enqueue(E.structured_directive(
                    sid, event_type, text, source_event_id=user_event["event_id"],
                    project_id=pid, channel=self._platform))
        if assistant_content:
            self._enqueue(E.assistant_message(sid, assistant_content,
                                              project_id=pid,
                                              channel=self._platform))
        # derive tool/shell/file/test events from the OpenAI message list
        for ev in _derive_tool_events(sid, messages or [], pid, self._platform):
            self._enqueue(ev)
        self._enqueue(E.turn_completed(
            sid, self._turn_number, project_id=pid, channel=self._platform))
        _trace("SYNC_TURN_END", session_id=sid, status="ok")

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        _trace("ON_SESSION_END_START", session_id=self._session_id, status="start")
        if self._agent_context != "primary":
            return
        # final flush of any remaining tool events + close
        for ev in _derive_tool_events(self._session_id, messages,
                                      self._project_id, self._platform):
            self._enqueue(ev)
        _trace("ON_SESSION_END_END", session_id=self._session_id, status="ok")

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        self._session_id = new_session_id
        if reset:
            self._cache.clear()
            self._off = False
        self._project_id = None
        try:
            r = self._client.resolve_project(session_id=new_session_id)
            self._project_id = r.get("project_id")
        except Exception:
            pass

    def _enqueue(self, event: dict) -> None:
        """Durable local append; async sender drains to MegaBrain."""
        if self._outbox is None:
            return
        _trace("OUTBOX_APPEND_START", session_id=event.get("session_id"), event_type=event.get("event_type"))
        try:
            inserted = self._outbox.append(event)
            _trace("outbox_append", session_id=event.get("session_id"), event_type=event.get("event_type"), outbox_action="inserted" if inserted else "duplicate")
        except Exception:
            _trace("outbox_append", session_id=event.get("session_id"), event_type=event.get("event_type"), outbox_action="error")
            # never break a turn on outbox failure
        _trace("OUTBOX_APPEND_END", session_id=event.get("session_id"), event_type=event.get("event_type"), status="ok")

    # -- tools -------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {"name": "memory_status", "description": "Показать состояние памяти MegaBrain (проект, режим, outbox).",
             "parameters": {"type": "object", "properties": {}, "required": []}},
            {"name": "memory_project", "description": "Показать/задать/сбросить проект памяти.",
             "parameters": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["show", "set", "clear"]},
                 "project_id": {"type": "string"},
                 "name": {"type": "string"}}, "required": ["action"]}},
            {"name": "memory_search", "description": "Поиск по памяти (FTS+vector+temporal).",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string"}, "mode": {"type": "string"}},
                 "required": ["query"]}},
            {"name": "memory_context", "description": "Показать Context Capsule текущего проекта.",
             "parameters": {"type": "object", "properties": {}, "required": []}},
            {"name": "memory_off", "description": "Выключить инжект памяти на текущую сессию.",
             "parameters": {"type": "object", "properties": {}, "required": []}},
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        import json
        try:
            if tool_name == "memory_off":
                self._off = True
                return json.dumps({"ok": True, "memory": "off"})
            if tool_name == "memory_status":
                return json.dumps({
                    "provider": "megabrain",
                    "project_id": self._project_id,
                    "off": self._off,
                    "outbox": self._outbox.counts() if self._outbox else {},
                }, ensure_ascii=False)
            if tool_name == "memory_project":
                return self._tool_project(args)
            if tool_name == "memory_context":
                if not self._project_id:
                    return json.dumps({"ok": False, "error": "project not resolved"})
                cap = self._client.get_context(project_id=self._project_id)
                return json.dumps(cap, ensure_ascii=False, default=str)[:8000]
            if tool_name == "memory_search":
                if getattr(self, "_current_mode", None) == "HOT":
                    return json.dumps({"ok": True, "mode": "HOT", "results": [], "note": "structured capsule already injected"})
                q = args.get("query", "")
                mode = args.get("mode") or select_mode(q)
                r = self._client.memory_search(q, project_id=self._project_id,
                                               mode=mode)
                return json.dumps(r, ensure_ascii=False, default=str)[:8000]
            return json.dumps({"ok": False, "error": f"unknown tool {tool_name}"})
        except MegaBrainUnavailable:
            return json.dumps({"ok": False, "error": "megabrain unavailable"})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)[:200]})

    def _tool_project(self, args: dict[str, Any]) -> str:
        import json
        action = args.get("action")
        if action == "show":
            return json.dumps({"project_id": self._project_id})
        if action == "set":
            pid = args.get("project_id")
            if not pid:
                return json.dumps({"ok": False, "error": "project_id required"})
            self._client.resolve_project(project_id=pid, session_id=self._session_id,
                                         source="hermes", profile=self._profile, channel=self._channel,
                                         parent_session_id=self._parent_session_id,
                                         conversation_id=self._conversation_id)
            self._project_id = pid
            return json.dumps({"ok": True, "project_id": pid})
        if action == "clear":
            self._project_id = None
            self._cache.clear()
            return json.dumps({"ok": True, "project_id": None})
        return json.dumps({"ok": False, "error": "unknown action"})

    # -- config ------------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "base_url", "description": "MegaBrain API URL",
             "default": "http://127.0.0.1:4300", "env_var": "MB_BASE_URL"},
            {"key": "api_token", "description": "MegaBrain API token (empty = auth off)",
             "secret": True, "env_var": "MB_API_TOKEN"},
        ]

    def shutdown(self) -> None:
        _trace("shutdown", session_id=self._session_id, status="called")
        if self._sender:
            self._sender.stop()
        if self._outbox:
            self._outbox.close()


# --- helpers ---------------------------------------------------------------

def _budget_for(mode: str) -> int:
    return {"NONE": 0, "HOT": 6000, "WARM": 6000, "DEEP": 6000}.get(mode, 6000)


def _format_capsule(cap: dict, mode: str) -> str:
    if not cap:
        return ""
    lines = ["[Память MegaBrain]"]
    order = ["constraints", "current_state", "open_work", "confirmed_decisions",
             "recent_changes", "known_failures", "important_facts",
             "relevant_experience"]
    for section in order:
        items = cap.get(section)
        if not items:
            continue
        title = section.replace("_", " ").title()
        lines.append(f"\n## {title}")
        for it in items[:8]:
            if isinstance(it, dict):
                c = it.get("content") or {}
                text = c.get("text") or c.get("summary") or c.get("title") or ""
                if text:
                    lines.append(f"- {str(text)[:400]}")
            else:
                lines.append(f"- {str(it)[:400]}")
    return "\n".join(lines)


def _format_evidence(search: dict) -> str:
    results = search.get("results", [])
    if not results:
        return ""
    lines = ["\n## Relevant history"]
    for r in results[:5]:
        text = r.get("text") or r.get("content") or r.get("summary") or ""
        if text:
            lines.append(f"- {str(text)[:400]}")
    return "\n".join(lines)


def _derive_tool_events(session_id: str, messages: list[dict[str, Any]],
                        project_id: str | None, channel: str) -> list[dict]:
    """Extract tool/shell/file/test/error events from the OpenAI message list."""
    out = []
    seen = set()
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls", []) or []:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = fn.get("arguments") or "{}"
                    if isinstance(args, str):
                        import json as _j
                        args = _j.loads(args) if args else {}
                except Exception:
                    args = {}
                ev = E.tool_call(session_id, name, args, project_id=project_id,
                                 channel=channel)
                if ev["event_id"] not in seen:
                    seen.add(ev["event_id"]); out.append(ev)
                if name in ("run_terminal", "terminal", "execute_command"):
                    cmd = args.get("command") or args.get("cmd") or ""
                    if cmd:
                        se = E.shell_command(session_id, cmd, project_id=project_id,
                                             channel=channel)
                        if se["event_id"] not in seen:
                            seen.add(se["event_id"]); out.append(se)
        elif role == "tool":
            name = m.get("name") or ""
            content = m.get("content") or ""
            is_error = m.get("is_error") or (isinstance(content, str) and
                                             ("Error" in content or "error" in content[:80]))
            ev = E.tool_result(session_id, name, str(content)[:4000],
                               project_id=project_id, channel=channel,
                               is_error=bool(is_error))
            if ev["event_id"] not in seen:
                seen.add(ev["event_id"]); out.append(ev)
    return out


def register(ctx) -> None:
    ctx.register_memory_provider(MegaBrainProvider())
