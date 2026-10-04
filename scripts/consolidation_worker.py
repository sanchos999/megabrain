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
SLEEP_S = float(os.environ.get("MB_CONSOLIDATION_TICK_S", "60.0"))

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
            result = worker.run_once()
            if result.get("status") == "failed":
                print(f"consolidation batch failed (error_class={result.get('error', 'unknown')})",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            # Exception strings can contain provider details or source text;
            # keep operational visibility without leaking those into journals.
            print(f"run_once error (error_class={type(e).__name__})", flush=True)
        time.sleep(SLEEP_S)
    print("consolidation worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
