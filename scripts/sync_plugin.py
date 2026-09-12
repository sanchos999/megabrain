"""Sync canonical integrations/hermes modules into the Hermes plugin dir.

Keeps a single source of truth (integrations/hermes/) and a byte-identical
vendored copy in $HERMES_HOME/plugins/megabrain/. Run from megabrain root:
    .venv/bin/python scripts/sync_plugin.py [--hermes-home ~/.hermes]
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "integrations" / "hermes"
# vendored name -> canonical name. events.py is renamed to mb_events.py in the
# plugin dir to avoid colliding with megabrain's own `events` package.
MODULES = {
    "megabrain_client.py": "megabrain_client.py",
    "outbox.py": "outbox.py",
    "sender.py": "sender.py",
    "mb_events.py": "events.py",
}


def main() -> int:
    hermes_home = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / ".hermes"
    dst = hermes_home / "plugins" / "megabrain"
    dst.mkdir(parents=True, exist_ok=True)
    for vendored, canonical in MODULES.items():
        shutil.copyfile(SRC / canonical, dst / vendored)
        print(f"sync {canonical} -> {dst / vendored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
