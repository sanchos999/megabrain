#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--purge-data" ]]; then
  echo 'Refusing implicit data purge; review and remove data explicitly.' >&2
  exit 2
fi
systemctl --user stop megabrain.service megabrain-embedding-worker.service megabrain-consolidation-worker.service megabrain-hermes-outbox.service 2>/dev/null || true
echo 'services stopped; user data preserved'
