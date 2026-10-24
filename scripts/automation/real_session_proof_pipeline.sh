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
    echo "[SKIP] Weekend real-session proof pipeline skipped."
    exit 0
  fi
fi

REPLICA_STRATEGY_ID="${REPLICA_STRATEGY_ID:-index_super_win_replica_001}"
QQQ_STRATEGY_ID="${QQQ_STRATEGY_ID:-qqq_intraday_001}"
MIN_TOTAL_SAMPLES="${MIN_TOTAL_SAMPLES:-1}"
MIN_BLOCKED_SAMPLES="${MIN_BLOCKED_SAMPLES:-1}"
QTY_TOLERANCE="${QTY_TOLERANCE:-1.0}"

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/replay/run_real_session_proof_pipeline.py" \
  --replica-strategy-id "$REPLICA_STRATEGY_ID" \
  --qqq-strategy-id "$QQQ_STRATEGY_ID" \
  --min-total-samples "$MIN_TOTAL_SAMPLES" \
  --min-blocked-samples "$MIN_BLOCKED_SAMPLES" \
  --qty-tolerance "$QTY_TOLERANCE"
