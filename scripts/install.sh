#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -m venv "${MEGABRAIN_VENV:-.venv}"
"${MEGABRAIN_VENV:-.venv}/bin/python" -m pip install -U pip
"${MEGABRAIN_VENV:-.venv}/bin/python" -m pip install -e '.[dev]'
"${MEGABRAIN_VENV:-.venv}/bin/python" scripts/migrate.py
"${MEGABRAIN_VENV:-.venv}/bin/python" -m uvicorn api.main:app --host "${MEGABRAIN_HOST:-127.0.0.1}" --port "${MEGABRAIN_PORT:-4300}" &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -fsS "http://${MEGABRAIN_HOST:-127.0.0.1}:${MEGABRAIN_PORT:-4300}/health" >/dev/null && exit 0; sleep 1; done
exit 1
