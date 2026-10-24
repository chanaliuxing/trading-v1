#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"

PY="venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "[start] missing venv python: $PY"
  exit 1
fi

echo "[start] checking options_quotes_1m health (QQQ)..."
if ! PYTHONPATH="$REPO_DIR" "$PY" scripts/options/check_options_quotes_health.py --underlying QQQ --max-age-min 3.0; then
  echo "[start] WARN options quotes health check failed; continue startup so stock strategies remain available"
fi

echo "[start] launching run_system.py"
PYTHONPATH="$REPO_DIR" exec "$PY" run_system.py
