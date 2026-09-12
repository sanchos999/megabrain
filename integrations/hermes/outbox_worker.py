#!/usr/bin/env python3
"""Persistent r7canary MegaBrain outbox sender."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from megabrain_client import MegaBrainClient
from outbox import Outbox
from sender import Sender


def main() -> None:
    base_url = os.environ.get("MB_BASE_URL", "http://127.0.0.1:4300")
    token_path = Path(os.environ.get("MB_API_TOKEN_FILE", "~/.config/megabrain/token")).expanduser()
    token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
    outbox = Outbox(Path(os.environ.get("MB_OUTBOX_PATH", "~/.hermes/profiles/r7canary/megabrain-outbox.db")).expanduser())
    sender = Sender(MegaBrainClient(base_url, token=token, timeout=5.0), outbox)
    sender.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        sender.stop()
        outbox.close()


if __name__ == "__main__":
    main()
