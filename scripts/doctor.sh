#!/usr/bin/env bash
set -euo pipefail
base="http://${MEGABRAIN_HOST:-127.0.0.1}:${MEGABRAIN_PORT:-4300}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user_root="$(dirname "$root")"
env_file="${MEGABRAIN_ENV_FILE:-$user_root/.config/megabrain/production.env}"
if [ -f "$env_file" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$env_file"
  set +a
fi
check(){ printf '%-14s' "$1"; shift; if "$@" >/dev/null 2>&1; then echo PASS; else echo FAIL; fi; }
redis_check(){
  "$root/.venv/bin/python" - "$1" <<'PY'
import socket
import sys
from urllib.parse import urlparse

u = urlparse(sys.argv[1])
host = u.hostname or "127.0.0.1"
port = u.port or 6379
password = u.password
with socket.create_connection((host, port), timeout=3) as s:
    if password:
        s.sendall(f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n".encode())
        if not s.recv(128).startswith(b"+OK"):
            raise SystemExit(1)
    s.sendall(b"*1\r\n$4\r\nPING\r\n")
    if not s.recv(128).startswith(b"+PONG"):
        raise SystemExit(1)
PY
}
check api curl -fsS "$base/health"
check postgres pg_isready -h "${PGHOST:-127.0.0.1}" -p "${PGPORT:-5432}"
check redis redis_check "${MEGABRAIN_REDIS_URL:-redis://127.0.0.1:6390/0}"
model_dir="${MB_ONNX_MODEL_DIR:-${MEGABRAIN_MODEL_DIR:-$root/models/bge-m3}}"
check model test -s "$model_dir/onnx/model_quantized.onnx" && test -s "$model_dir/tokenizer.json"
check disk test "$(df -P . | awk 'NR==2 {print $4}')" -gt 1048576
