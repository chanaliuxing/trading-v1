#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

DAY="${DAY:-$(date +%F)}"
AUDIT_ROOT="${AUDIT_ROOT:-$PROJECT_ROOT/runtime/audit}"
STRATEGY_ID="${STRATEGY_ID:-index_super_win_001}"
OUTPUT_PATH="${OUTPUT_PATH:-$PROJECT_ROOT/runtime/state/index_super_win_trace_smoke_gate.json}"

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/replay/build_replica_trace_smoke_gate.py" \
  --day "$DAY" \
  --audit-root "$AUDIT_ROOT" \
  --strategy-id "$STRATEGY_ID" \
  --output-path "$OUTPUT_PATH"
