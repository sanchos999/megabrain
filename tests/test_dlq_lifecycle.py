"""Dead-letter lifecycle (0.1.2): states, acknowledge, replay, stats; SQLite tmp."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrations" / "hermes"))
from outbox import Outbox  # noqa: E402


def make(tmp_path, n=3):
    box = Outbox(tmp_path / "outbox.db")
    for i in range(n):
        event = {"event_id": f"mb_{i}", "event_type": "USER_MESSAGE", "project_id": "p",
                 "payload": {"text": f"event {i}"}, "source": "test"}
        box.append(event)
        box.dead_letter(f"mb_{i}", last_error="HTTP 400", reason="PERMANENT_NON_RETRYABLE")
    return box


def test_dlq_lifecycle_acknowledge_and_stats(tmp_path):
    box = make(tmp_path)
    stats = box.dead_letter_stats()
    assert stats["total"] == 3 and stats["open"] == 3 and stats["acknowledged"] == 0
    assert box.acknowledge("mb_0", "historical_permanent")
    assert not box.acknowledge("mb_0"), "double acknowledge is a no-op"
    stats = box.dead_letter_stats()
    assert stats["open"] == 2 and stats["acknowledged"] == 1
    assert stats["new_7d"] == 2, "acknowledged historical letters are not current"


def test_dlq_replay_requeues_payload(tmp_path):
    box = make(tmp_path)
    assert box.replay("mb_1")
    pending = box.pending(10)
    assert [e["event_id"] for e in pending] == ["mb_1"]
    stats = box.dead_letter_stats()
    assert stats["replayed"] == 1 and stats["open"] == 2
    # replay of a replayed item is explicit and possible (still present)
    assert box.replay("mb_1")


def test_dlq_resolve_terminal(tmp_path):
    box = make(tmp_path)
    box.acknowledge("mb_2")
    assert box.resolve("mb_2")
    stats = box.dead_letter_stats()
    assert stats["resolved"] == 1 and stats["acknowledged"] == 0


def test_legacy_schema_migrates_on_open(tmp_path):
    box = make(tmp_path)
    box.close()
    import sqlite3

    db = sqlite3.connect(tmp_path / "outbox.db")
    db.execute("ALTER TABLE dead_letters DROP COLUMN state")
    db.commit(); db.close()
    box2 = Outbox(tmp_path / "outbox.db")  # re-open migrates
    assert box2.dead_letter_stats()["total"] == 3
    box2.close()


def test_sender_heartbeat_writes_durable_state(tmp_path):
    """Outbox sender heartbeat via MegaBrain API: unit-level check that the
    client posts the right payload and errors never kill delivery."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append((self.path, json.loads(self.rfile.read(length))))
            self.send_response(200); self.send_header("Content-Length", "2")
            self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *args): pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from megabrain_client import MegaBrainClient
        from sender import Sender

        box = Outbox(tmp_path / "hb.db")
        sender = Sender(MegaBrainClient(f"http://127.0.0.1:{server.server_port}", timeout=2.0), box)
        sender.heartbeat_interval_s = 0
        sender._heartbeat("IDLE")
        sender._heartbeat("RUNNING")
        sender._last_error_class = "MegaBrainUnavailable"
        sender._heartbeat("DEGRADED")
        paths = [r[0] for r in received]
        assert paths.count("/v1/worker/heartbeat") == 3
        assert received[-1][1]["component"] == "outbox"
        assert received[-1][1]["error_class"] == "MegaBrainUnavailable"
        box.close()
    finally:
        server.shutdown()
