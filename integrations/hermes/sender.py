"""Async sender: drain the local outbox into MegaBrain.

Runs in a daemon thread. On MegaBrain unavailability it stops the drain loop and
leaves rows in 'failed' status with backoff; the outbox is replayed on the next
flush. Non-blocking by design — the caller never waits on a network round-trip.

Pure stdlib + the vendored client. No megabrain imports.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

_TRACE_PATH = ""


def _trace(hook: str, **fields) -> None:
    if not _TRACE_PATH:
        return
    row = {"timestamp": datetime.now().astimezone().isoformat(), "hook": hook}
    for key in ("session_id", "turn_id", "event_type", "outbox_action", "http_status", "status"):
        if key in fields and fields[key] is not None:
            row[key] = fields[key]
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\\n")
    except Exception:
        pass

try:
    from .megabrain_client import MegaBrainClient, MegaBrainError, MegaBrainUnavailable
    from .outbox import Outbox
except ImportError:  # vendored flat copy (Hermes plugin dir)
    from megabrain_client import MegaBrainClient, MegaBrainError, MegaBrainUnavailable
    from outbox import Outbox

logger = logging.getLogger("megabrain.sender")


class Sender:
    def __init__(self, client: MegaBrainClient, outbox: Outbox,
                 batch_size: int = 50):
        self.client = client
        self.outbox = outbox
        self.batch_size = batch_size
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="megabrain-sender")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def flush_once(self) -> int:
        """Deliver one batch synchronously. Returns delivered count. Never raises."""
        delivered = 0
        try:
            for event in self.outbox.pending(self.batch_size):
                _trace("sender_attempt", session_id=event.get("session_id"), event_type=event.get("event_type"), outbox_action="send")
                eid = event["event_id"]
                self.outbox.mark_sending(eid)
                try:
                    self.client.write_event(event)
                    self.outbox.mark_delivered(eid)
                    _trace("sender_delivered", session_id=event.get("session_id"), event_type=event.get("event_type"), outbox_action="delivered", status="ok")
                    delivered += 1
                except (MegaBrainUnavailable, MegaBrainError) as e:
                    if event.get("event_type") == "TURN_STARTED" and not event.get("project_id"):
                        self.outbox.dead_letter(eid, last_error=str(e), reason="PERMANENT_NON_RETRYABLE_HISTORICAL_MISSING_PROJECT")
                        logger.warning("dead-lettered historical identity-less event %s", eid)
                        continue
                    if isinstance(e, MegaBrainError) and not isinstance(e, MegaBrainUnavailable) and 400 <= e.status < 500:
                        self.outbox.dead_letter(eid, last_error=str(e), reason="PERMANENT_NON_RETRYABLE")
                        logger.warning("dead-lettered %s: %s", eid, e)
                        continue
                    self.outbox.mark_failed(eid)
                    _trace("sender_failed", session_id=event.get("session_id"), event_type=event.get("event_type"), outbox_action="failed", status=type(e).__name__)
                    logger.warning("delivery failed %s: %s", eid, e)
                    # stop draining on connectivity failure; retry later
                    if isinstance(e, MegaBrainUnavailable):
                        break
        except Exception as e:
            logger.exception("sender flush error: %s", e)
        return delivered

    def _run(self) -> None:
        while not self._stop.is_set():
            n = self.flush_once()
            if n == 0:
                # idle: sleep a bit; pending rows with backoff will retry later
                self._stop.wait(2.0)
