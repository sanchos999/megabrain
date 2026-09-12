#!/usr/bin/env bash
set -euo pipefail
base="http://${MEGABRAIN_HOST:-127.0.0.1}:${MEGABRAIN_PORT:-4300}"
check(){ printf '%-14s' "$1"; shift; if "$@" >/dev/null 2>&1; then echo PASS; else echo FAIL; fi; }
check api curl -fsS "$base/health"
check postgres pg_isready -h "${PGHOST:-127.0.0.1}" -p "${PGPORT:-5432}"
check redis redis-cli -u "${MEGABRAIN_REDIS_URL:-redis://127.0.0.1:6390/0}" ping
check model test -d "${MEGABRAIN_MODEL_DIR:-models}"
check disk test "$(df -P . | awk 'NR==2 {print $4}')" -gt 1048576
