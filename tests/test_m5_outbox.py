"""Tests for the M5 outbox + sender + event builders (no network needed)."""

import pytest

from integrations.hermes import Outbox, Sender
from integrations.hermes import events as E
from integrations.hermes.megabrain_client import MegaBrainUnavailable


@pytest.fixture
def outbox(tmp_path):
    return Outbox(tmp_path / "outbox.db")


def test_append_is_durable_and_idempotent(outbox):
    ev = E.user_message("s1", "hello", channel="cli")
    assert outbox.append(ev) is True
    assert outbox.append(ev) is False  # duplicate, idempotent
    c = outbox.counts()
    assert c["pending"] == 1
    assert c["total"] == 1


def test_pending_returns_serialized_events(outbox):
    ev = E.user_message("s1", "hello")
    outbox.append(ev)
    got = outbox.pending()
    assert len(got) == 1
    assert got[0]["event_id"] == ev["event_id"]
    assert got[0]["event_type"] == "USER_MESSAGE"
    assert got[0]["payload"]["text"] == "hello"


def test_mark_failed_backoff_then_retryable(outbox):
    ev = E.user_message("s1", "hello")
    outbox.append(ev)
    outbox.mark_sending(ev["event_id"])
    outbox.mark_failed(ev["event_id"])
    # immediate retry blocked by backoff
    assert outbox.pending() == []
    # after clearing backoff window manually -> retryable
    outbox._conn.execute("UPDATE outbox SET next_retry=0 WHERE event_id=?",
                         (ev["event_id"],))
    outbox._conn.commit()
    assert len(outbox.pending()) == 1


def test_mark_delivered_removes_row(outbox):
    ev = E.user_message("s1", "hello")
    outbox.append(ev)
    outbox.mark_delivered(ev["event_id"])
    assert outbox.counts()["total"] == 0


def test_sender_drains_and_stops_on_unavailable(tmp_path):
    class FlakyClient:
        def __init__(self):
            self.calls = 0
        def write_event(self, event):
            self.calls += 1
            raise MegaBrainUnavailable(0, "down")

    ob = Outbox(tmp_path / "o.db")
    for i in range(3):
        ob.append(E.user_message("s1", f"m{i}"))
    s = Sender(FlakyClient(), ob)
    n = s.flush_once()
    assert n == 0
    assert ob.counts()["failed"] == 1  # first delivery fails, marks failed, stops
    # remaining still pending (not marked failed)
    assert ob.counts()["pending"] == 2


def test_sender_delivers_successfully(tmp_path):
    class OkClient:
        def __init__(self):
            self.seen = []
        def write_event(self, event):
            self.seen.append(event["event_id"])
            return {"accepted": True, "durable": True}

    ob = Outbox(tmp_path / "o.db")
    evs = [E.user_message("s1", f"m{i}") for i in range(3)]
    for e in evs:
        ob.append(e)
    c = OkClient()
    s = Sender(c, ob)
    n = s.flush_once()
    assert n == 3
    assert ob.counts()["total"] == 0
    assert set(c.seen) == {e["event_id"] for e in evs}


def test_event_ids_are_deterministic():
    a = E.user_message("s1", "same text")
    b = E.user_message("s1", "same text")
    c = E.user_message("s1", "different")
    assert a["event_id"] == b["event_id"]
    assert a["event_id"] != c["event_id"]


def test_events_carry_channel_and_session():
    ev = E.user_message("sess-1", "hi", channel="telegram")
    assert ev["session_id"] == "sess-1"
    assert ev["metadata"]["channel"] == "telegram"
    assert ev["source"] == "hermes"
    assert ev["event_type"] == "USER_MESSAGE"
