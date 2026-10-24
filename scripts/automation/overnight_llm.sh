#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/llm_trader"
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5
