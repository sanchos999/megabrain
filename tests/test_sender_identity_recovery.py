from integrations.hermes import mb_events
from integrations.hermes.megabrain_client import MegaBrainError
from integrations.hermes.outbox import Outbox
from integrations.hermes.sender import Sender


def test_replayed_legacy_turn_start_collision_is_rekeyed_and_preserved(tmp_path):
    session_id, turn_number, message = "session-resumed", 7, "new turn text"
    event = mb_events.turn_started(session_id, turn_number, message, project_id="project-a")
    original_id = mb_events._event_id("TURN_STARTED", session_id, str(turn_number))
    event["event_id"] = original_id
    new_id = mb_events.turn_started(session_id, turn_number, message)["event_id"]
    box = Outbox(tmp_path / "outbox.db")
    assert box.append(event)
    box.dead_letter(original_id, last_error="HTTP 400: exists with different payload_hash",
                    reason="PERMANENT_NON_RETRYABLE")
    assert box.replay(original_id)

    class Client:
        def __init__(self):
            self.sent = []

        def write_event(self, sent):
            self.sent.append(dict(sent))
            if len(self.sent) == 1:
                raise MegaBrainError(400, '{"detail":"event_id exists with different payload_hash"}')
            return {"accepted": True}

    client = Client()
    sender = Sender(client, box)

    assert sender.flush_once() == 1
    assert [sent["event_id"] for sent in client.sent] == [original_id, new_id]
    repaired = client.sent[1]
    assert repaired["payload"] == event["payload"]
    assert repaired["metadata"]["legacy_event_id"] == original_id
    assert repaired["correlation_id"] == original_id
    stats = box.dead_letter_stats()
    assert stats["open"] == 0
    assert stats["resolved"] == 1
    assert box._conn.execute(
        "select resolution from dead_letters where event_id=?", (original_id,)
    ).fetchone()[0] == f"rekeyed:{new_id}"
    box.close()
