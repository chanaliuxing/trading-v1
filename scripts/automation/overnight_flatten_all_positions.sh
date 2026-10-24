#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-.}"
cd "$PROJECT_ROOT"

if [[ "${OVERNIGHT_FLATTEN_BYPASS_TIME_GUARD:-0}" != "1" ]]; then
  "$PROJECT_ROOT/venv/bin/python" - <<'PY'
from datetime import datetime, time
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("America/New_York"))
allowed = now.time() >= time(20, 0) or now.time() < time(3, 50)
if not allowed:
    raise SystemExit(f"overnight_flatten_time_guard_blocked:now_et={now.isoformat(timespec='seconds')}")
PY
fi

DAY_ID="$(/bin/date +%Y%m%d)"
LOG_DIR="$PROJECT_ROOT/logs"
REPORT_DIR="$PROJECT_ROOT/reports/runtime_reconciliation"
mkdir -p "$LOG_DIR" "$REPORT_DIR"

# Ensure repo-local logging path (avoids writing outside the workspace).
export AUDIT_LOG_PATH="${AUDIT_LOG_PATH:-$LOG_DIR/trader.log}"

SNAPSHOT_BEFORE="$REPORT_DIR/overnight_flatten_before_${DAY_ID}.json"
SNAPSHOT_AFTER="$REPORT_DIR/overnight_flatten_after_${DAY_ID}.json"
PLAN_PATH="$REPORT_DIR/overnight_flatten_plan_${DAY_ID}.json"
EXEC_PATH="$REPORT_DIR/overnight_flatten_execute_${DAY_ID}.json"
LOG_PATH="$LOG_DIR/overnight_flatten_all_positions_${DAY_ID}.log"

{
  echo "[START] $(/bin/date '+%Y-%m-%d %H:%M:%S %Z')"

  "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/broker_truth_convergence.py" live-snapshot \
    --connect-cooldown-sec 0 \
    --output "$SNAPSHOT_BEFORE"

  "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/cleanup_strategy_residuals.py" \
    --symbols QQQ TQQQ SQQQ SOXS SOXL SMH SPY SPXL SPXS RSP \
    --strategy-ids qqq_intraday_001 qqq_intraday_option_v1_001 qqq_1dte_ma10_reject_0324_v2_001 spy_1dte_ma10_reject_0324_001 stocks_kernel_shadow_smh_001 stocks_kernel_shadow_spy_001 qqq_kernel_shadow_legacy_on_001 qqq_kernel_shadow_legacy_off_001 \
    --primary-strategy-id qqq_intraday_001 \
    --session overnight \
    --connect-cooldown-sec 0 \
    --audit-root-dir "$PROJECT_ROOT/runtime/audit" \
    --dry-run | tee "$PLAN_PATH"

  "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/cleanup_strategy_residuals.py" \
    --symbols QQQ TQQQ SQQQ SOXS SOXL SMH SPY SPXL SPXS RSP \
    --strategy-ids qqq_intraday_001 qqq_intraday_option_v1_001 qqq_1dte_ma10_reject_0324_v2_001 spy_1dte_ma10_reject_0324_001 stocks_kernel_shadow_smh_001 stocks_kernel_shadow_spy_001 qqq_kernel_shadow_legacy_on_001 qqq_kernel_shadow_legacy_off_001 \
    --primary-strategy-id qqq_intraday_001 \
    --session overnight \
    --connect-cooldown-sec 0 \
    --audit-root-dir "$PROJECT_ROOT/runtime/audit" | tee "$EXEC_PATH" || true

  /bin/sleep 30

  "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/broker_truth_convergence.py" live-snapshot \
    --connect-cooldown-sec 0 \
    --output "$SNAPSHOT_AFTER"

  "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/ops/broker_truth_convergence.py" inspect-active

  echo "[END] $(/bin/date '+%Y-%m-%d %H:%M:%S %Z')"
} 2>&1 | tee -a "$LOG_PATH"
