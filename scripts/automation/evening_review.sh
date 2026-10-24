#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/docs/daily"
./evening_review.sh || true

# Optional: nightly knowledge consolidation (heuristics) and evolution sandbox
if [ "${ENABLE_CRITIC_CONSOLIDATE:-0}" = "1" ]; then
  cd "$PROJECT_ROOT"
  python scripts/ai_trader/memory/consolidate_knowledge.py || true
fi

if [ "${ENABLE_EVOLVER_SANDBOX:-0}" = "1" ]; then
  cd "$PROJECT_ROOT"
  python scripts/ai_trader/evolvers/code_evolver_git.py --skill dummy_math || true
  python scripts/ai_trader/evolvers/import_evolution_experiments.py || true
fi
