#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CONFIG_PATH="${CONFIG_PATH:-$PROJECT_ROOT/config.yml}"
STRATEGY_ID="${STRATEGY_ID:-index_super_win_001}"
PREFLIGHT_REPORT="${PREFLIGHT_REPORT:-$PROJECT_ROOT/runtime/state/index_super_win_replica_preflight.json}"
REAL_SESSION_SUMMARY="${REAL_SESSION_SUMMARY:-$PROJECT_ROOT/runtime/state/index_super_win_same_symbol_proof/real_session_proof_daily_summary.json}"
BATCH_REPORT="${BATCH_REPORT:-$PROJECT_ROOT/runtime/state/index_super_win_same_symbol_proof/real_session_dual_proof_batch_report.json}"
ISOLATION_DAILY_SUMMARY="${ISOLATION_DAILY_SUMMARY:-$PROJECT_ROOT/runtime/state/multi_strategy_isolation_daily_summary.json}"
TRACE_SMOKE_GATE="${TRACE_SMOKE_GATE:-$PROJECT_ROOT/runtime/state/index_super_win_trace_smoke_gate.json}"
OUTPUT_PATH="${OUTPUT_PATH:-$PROJECT_ROOT/runtime/state/index_super_win_same_symbol_proof/final_enablement_checklist.json}"
REQUIRED_GREEN_DAYS="${REQUIRED_GREEN_DAYS:-3}"
MAX_HIGH_VALUE_ISOLATION_EVENTS="${MAX_HIGH_VALUE_ISOLATION_EVENTS:-0}"

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/replay/build_replica_final_enablement_checklist.py" \
  --config "$CONFIG_PATH" \
  --strategy-id "$STRATEGY_ID" \
  --preflight-report "$PREFLIGHT_REPORT" \
  --real-session-summary "$REAL_SESSION_SUMMARY" \
  --batch-report "$BATCH_REPORT" \
  --isolation-daily-summary "$ISOLATION_DAILY_SUMMARY" \
  --trace-smoke-gate "$TRACE_SMOKE_GATE" \
  --output-path "$OUTPUT_PATH" \
  --required-green-days "$REQUIRED_GREEN_DAYS" \
  --max-high-value-isolation-events "$MAX_HIGH_VALUE_ISOLATION_EVENTS"
