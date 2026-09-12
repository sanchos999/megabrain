"""Test defaults are isolated from production auth and optional heavy integrations."""
from __future__ import annotations

import os
from pathlib import Path

# CI supplies its own database URL. On a developer machine, use the local runtime
# env only when explicitly present; never commit it.
if not os.environ.get("MEGABRAIN_DATABASE_URL"):
    runtime_env = Path.home() / ".config/megabrain/production.env"
    if runtime_env.is_file():
        for line in runtime_env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)
os.environ.setdefault("MEGABRAIN_API_TOKEN", "")
os.environ.setdefault("MEGABRAIN_API_TOKEN_FILE", "")
# Unit/API contract tests run in-process and must not inherit a developer's
# production auth environment. Auth is covered by the dedicated test below.
os.environ["MB_API_TOKEN"] = ""
os.environ["MB_API_TOKEN_FILE"] = ""
