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
    echo "[SKIP] Weekend isolation monitor run skipped."
    exit 0
  fi
fi

SNAPSHOT_INTERVAL_SEC="${SNAPSHOT_INTERVAL_SEC:-60}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"
ITERATIONS="${ITERATIONS:-5000}"
ALERTS_LIMIT="${ALERTS_LIMIT:-20}"

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/multi_strategy_isolation_monitor.py" \
  --iterations "$ITERATIONS" \
  --snapshot-interval-sec "$SNAPSHOT_INTERVAL_SEC" \
  --poll-interval-sec "$POLL_INTERVAL_SEC" \
  --alerts-limit "$ALERTS_LIMIT"
