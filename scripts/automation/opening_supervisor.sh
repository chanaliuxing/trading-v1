#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env"
  set +a
fi

if [[ -f "$PROJECT_ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/venv/bin/activate"
fi

cd "$PROJECT_ROOT"

if [[ "${RUN_ON_WEEKENDS:-0}" != "1" ]]; then
  weekday="$(date +%u)"
  if [[ "$weekday" -gt 5 ]]; then
    echo "[SKIP] Weekend opening supervisor skipped."
    exit 0
  fi
fi

PHASE="${PHASE:-open}"
AUTO_REMEDIATE="${AUTO_REMEDIATE:-0}"

ARGS=(--phase "$PHASE")
if [[ "$AUTO_REMEDIATE" == "1" ]]; then
  ARGS+=(--auto-remediate)
fi

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/run_opening_supervisor.py" "${ARGS[@]}"
