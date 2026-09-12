"""Tests for the M5 Hermes plugin (mode selector, event derivation, channel).

Loads the vendored plugin module in isolation (flat imports) without the full
Hermes runtime, so these tests run in the megabrain venv.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes" / "megabrain"
INIT = PLUGIN_DIR / "__init__.py"


def _load_plugin():
    # agent.memory_provider is stdlib-only; resolve it from the hermes-agent tree
    sys.path.insert(0, str(PLUGIN_DIR))
    
    spec = importlib.util.spec_from_file_location(
        "_hermes_user_memory.megabrain", str(INIT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_plugin()


def test_select_mode(mod):
    assert mod.select_mode("") == "NONE"
    assert mod.select_mode("привет") == "NONE"
    assert mod.select_mode("ок") == "NONE"
    assert mod.select_mode("продолжаем") == "HOT"
    assert mod.select_mode("что осталось") == "HOT"
    assert mod.select_mode("какая ошибка была") == "WARM"
    assert mod.select_mode("что мы решили") == "WARM"
    assert mod.select_mode("за всю историю") == "DEEP"
    assert mod.select_mode("в других проектах") == "DEEP"


def test_channel_mapping(mod):
    assert mod._channel_from_kwargs("", "telegram") == "telegram"
    assert mod._channel_from_kwargs("subagent", "") == "subagent"
    assert mod._channel_from_kwargs("cron", "") == "cron"
    assert mod._channel_from_kwargs("", "") == "cli"


def test_derive_tool_events(mod):
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "run_terminal",
                          "arguments": json.dumps({"command": "ls -la"})}}]},
        {"role": "tool", "name": "run_terminal", "content": "total 4\n"},
        {"role": "tool", "name": "read_file", "content": "Error: not found"},
    ]
    evs = mod._derive_tool_events("s1", messages, "p1", "cli")
    types = {e["event_type"] for e in evs}
    assert "TOOL_CALL" in types
    assert "SHELL_COMMAND" in types
    assert "TOOL_RESULT" in types
    # error flag on the error tool result
    err = [e for e in evs if e["event_type"] == "TOOL_RESULT"
           and e["payload"]["tool"] == "read_file"][0]
    assert err["payload"]["is_error"] is True


def test_derive_no_duplicate_event_ids(mod):
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "run_terminal",
                          "arguments": json.dumps({"command": "ls"})}},
            {"function": {"name": "run_terminal",
                          "arguments": json.dumps({"command": "ls"})}}]},
    ]
    evs = mod._derive_tool_events("s1", messages, None, "cli")
    ids = [e["event_id"] for e in evs if e["event_type"] == "TOOL_CALL"]
    assert len(ids) == len(set(ids))  # dedup identical calls
