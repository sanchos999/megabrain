"""Sync canonical integrations/hermes modules into the Hermes plugin dir.

Keeps a single source of truth (integrations/hermes/) and a byte-identical
vendored copy in $HERMES_HOME/plugins/megabrain/. Run from megabrain root:
    .venv/bin/python scripts/sync_plugin.py [--hermes-home ~/.hermes]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "integrations" / "hermes"
# vendored name -> canonical name. mb_events.py avoids colliding with
# MegaBrain's own `events` package; sync it from the canonical mb_events module.
MODULES = {
    "__init__.py": "__init__.py",
    "README.md": "README.md",
    "plugin.yaml": "plugin.yaml",
    "megabrain_client.py": "megabrain_client.py",
    "outbox.py": "outbox.py",
    "sender.py": "sender.py",
    "mb_events.py": "mb_events.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync MegaBrain Hermes plugin files")
    parser.add_argument("hermes_home_positional", nargs="?", help="Hermes home (legacy positional form)")
    parser.add_argument("--hermes-home", dest="hermes_home_option", help="Hermes home directory")
    args = parser.parse_args()
    hermes_home_value = args.hermes_home_option or args.hermes_home_positional
    hermes_home = Path(hermes_home_value).expanduser() if hermes_home_value else Path.home() / ".hermes"
    dst = hermes_home / "plugins" / "megabrain"
    dst.mkdir(parents=True, exist_ok=True)
    for vendored, canonical in MODULES.items():
        shutil.copyfile(SRC / canonical, dst / vendored)
        print(f"sync {canonical} -> {dst / vendored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
