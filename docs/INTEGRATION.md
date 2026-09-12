# Generic agent integration

MegaBrain is agent-neutral. An adapter passes identity and calls REST; it does not import Hermes or any framework.

Identity fields: optional `tenant_id`, `user_id`, required `agent_id` when available, `source`, `profile`, `channel`, `workspace/project_id`, `conversation_id`, and `session_id`.

Minimum adapter operations:

1. `POST /v1/events` for user/assistant/tool/decision/constraint/task events;
2. `POST /v1/resolve-project` with source/profile/channel/session identity;
3. `POST /v1/memory/context` with the resolved project;
4. `POST /v1/memory/search` with WARM or DEEP and provenance handling.

```python
from clients.python import MegaBrainClient

mb = MegaBrainClient("http://127.0.0.1:4300", token="")
mb.write_event({
    "source": "example-agent", "session_id": "s1", "project_id": "demo",
    "event_type": "DECISION", "created_at": "2026-01-01T00:00:00Z",
    "payload": {"item_key": "storage", "text": "Use PostgreSQL"},
})
print(mb.resolve(session_id="s1", source="example-agent", profile="default", channel="chat"))
print(mb.get_context(project_id="demo"))
```

The core is usable without Hermes installed. Hermes-specific defaults and the optional `should_recall` adapter live under `integrations/hermes/`.
