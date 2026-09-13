"""Consolidation worker entry point (systemd service, low priority).

Long-running: process one batch, sleep, repeat. SIGTERM-safe (finishes the
current batch). Single instance via flock. Resource limits are enforced by the
systemd unit (CPUQuota / MemoryMax / Nice), not here.
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from consolidation.worker import ConsolidationWorker

LOCK = Path(os.environ.get("MB_STATE_DIR") or (ROOT / "state")) / "consolidation-worker.lock"
SLEEP_S = float(os.environ.get("MB_CONSOLIDATION_SLEEP_S", "60.0"))
SLEEP_IDLE_S = float(os.environ.get("MB_CONSOLIDATION_IDLE_SLEEP_S", "300.0"))

_stop = False


def _handle_term(signum, frame):
    global _stop
    _stop = True


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another consolidation worker is running", flush=True)
        return 1
    worker = ConsolidationWorker()
    print(f"consolidation worker started (model={worker.__class__.__module__})", flush=True)
    while not _stop:
        try:
            res = worker.run_once()
        except Exception as e:  # noqa: BLE001
            print(f"run_once error: {str(e)[:200]}", flush=True)
            res = {"status": "error"}
        if res.get("status") == "idle":
            time.sleep(SLEEP_IDLE_S)
        else:
            time.sleep(SLEEP_S)
    print("consolidation worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
