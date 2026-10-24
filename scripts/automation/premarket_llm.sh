#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/llm_trader"
if [[ -x ./run_premarket.sh ]]; then
  ./run_premarket.sh
else
  # Fallback to a safe default with ET gating
  python run_single_bot.py \
    --provider deepseek_nvidia \
    --symbols TSLA \
    --interval 600 \
    --cycles 33 \
    --close-at "09:35" \
    --forbid-new-positions-before 9 \
    --wait-for-premarket
fi
