#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/llm_trader"
if [[ -x ./run_all_providers.sh ]]; then
  ./run_all_providers.sh market
else
  # Fallback to first-hour fast strategy
  python run_single_bot.py \
    --provider deepseek \
    --symbols TSLA \
    --interval 60 \
    --wait-for-market \
    --start-trading-at "9:40" \
    --stop-trading-at "10:30" \
    --close-at "10:45"
fi
