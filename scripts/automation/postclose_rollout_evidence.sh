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
    echo "[SKIP] Weekend postclose rollout evidence skipped."
    exit 0
  fi
fi

DAY_ARG="${DAY:-$(date +%Y%m%d)}"

"$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/build_daily_trading_evidence.py" --day "$DAY_ARG"
"$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/evaluate_rollout_state.py" --evidence "$PROJECT_ROOT/reports/daily_trading_evidence/${DAY_ARG}.json" --state "$PROJECT_ROOT/runtime/state/trading_rollout_state.json"
exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/update_rollout_orchestrator_state.py"
