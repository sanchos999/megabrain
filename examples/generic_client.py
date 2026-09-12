from clients.python import MegaBrainClient

client = MegaBrainClient("http://127.0.0.1:4300", token="")
project_id = "demo-project"
client.create_project(project_id, "Generic client demo")
client.write_event({
    "source": "example-agent",
    "session_id": "session-1",
    "project_id": project_id,
    "event_type": "DECISION",
    "created_at": "2026-01-01T00:00:00Z",
    "payload": {"item_key": "storage", "text": "Use PostgreSQL"},
})
print(client.resolve_project(session_id="session-1", source="example-agent", profile="default", channel="chat"))
print(client.get_context(project_id=project_id))
