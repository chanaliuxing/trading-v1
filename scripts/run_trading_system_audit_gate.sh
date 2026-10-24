#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-quick}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$MODE" in
  quick|full|pre_canary)
    ;;
  *)
    echo "usage: $0 [quick|full|pre_canary]" >&2
    exit 2
    ;;
esac

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PY="$ROOT_DIR/venv/bin/python"
elif [[ -x "$ROOT_DIR/venv_ml/bin/python" ]]; then
  PY="$ROOT_DIR/venv_ml/bin/python"
else
  PY="python3"
fi

DAY="${AUDIT_GATE_DAY:-$(date +%Y%m%d)}"
RESULT_DIR="$ROOT_DIR/reports/audit_gate"
mkdir -p "$RESULT_DIR"
COMMAND_RESULTS="$RESULT_DIR/audit_gate_command_results_${DAY}_$$.json"
printf '{"commands":[]}\n' > "$COMMAND_RESULTS"

append_result() {
  local name="$1"
  local command_text="$2"
  local returncode="$3"
  local duration="$4"
  local output_file="$5"
  local status="PASS"
  if [[ "$returncode" -ne 0 ]]; then
    status="FAIL"
  fi
  RESULT_NAME="$name" \
  RESULT_COMMAND="$command_text" \
  RESULT_RETURNCODE="$returncode" \
  RESULT_STATUS="$status" \
  RESULT_DURATION="$duration" \
  RESULT_OUTPUT_FILE="$output_file" \
  RESULT_JSON="$COMMAND_RESULTS" \
  "$PY" - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

path = Path(os.environ["RESULT_JSON"])
payload = json.loads(path.read_text(encoding="utf-8"))
out_path = Path(os.environ["RESULT_OUTPUT_FILE"])
try:
    lines = out_path.read_text(encoding="utf-8", errors="ignore").splitlines()
except OSError:
    lines = []
summary = "\n".join(lines[-8:])
payload.setdefault("commands", []).append(
    {
        "name": os.environ["RESULT_NAME"],
        "command": os.environ["RESULT_COMMAND"],
        "returncode": int(os.environ["RESULT_RETURNCODE"]),
        "status": os.environ["RESULT_STATUS"],
        "duration_sec": round(float(os.environ["RESULT_DURATION"]), 3),
        "summary": summary[-2000:],
    }
)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

run_step() {
  local name="$1"
  shift
  local output_file="$RESULT_DIR/${name}_${DAY}_$$.log"
  local start end duration rc
  start="$(date +%s)"
  echo "==> $name"
  TRADING_TEST_ALLOW_LIVE_IBKR=0 "$@" >"$output_file" 2>&1
  rc=$?
  end="$(date +%s)"
  duration=$((end - start))
  append_result "$name" "$*" "$rc" "$duration" "$output_file"
  if [[ "$rc" -ne 0 ]]; then
    echo "FAIL $name"
    tail -n 20 "$output_file" >&2
  else
    echo "PASS $name"
  fi
  return "$rc"
}

build_report() {
  "$PY" tool/report/build_trading_system_audit_gate_report.py \
    --repo-root "$ROOT_DIR" \
    --mode "$MODE" \
    --day "$DAY" \
    --command-results-json "$COMMAND_RESULTS"
}

FAILED=0

run_step no_live_guard "$PY" -m pytest -q tests/unit/safety/test_no_live_ibkr_in_unit_tests.py
NO_LIVE_RC="$?"
if [[ "$NO_LIVE_RC" -ne 0 ]]; then
  build_report
  exit "$NO_LIVE_RC"
fi

run_step git_diff_check git diff --check || FAILED=1
run_step exec_contract "$PY" -m pytest -q tests/unit/exec || FAILED=1
run_step services_contract "$PY" -m pytest -q tests/unit/services || FAILED=1
run_step ib_reconnect_fast scripts/run_ib_reconnect_regression.sh fast || FAILED=1

if [[ "$MODE" == "full" || "$MODE" == "pre_canary" ]]; then
  run_step reconciliation_contract "$PY" -m pytest -q tests/unit/reconciliation || FAILED=1
  run_step reports_contract "$PY" -m pytest -q tests/unit/reports || FAILED=1
  run_step ib_reconnect_full scripts/run_ib_reconnect_regression.sh full || FAILED=1
fi

if [[ "$MODE" == "pre_canary" ]]; then
  RUNTIME_DURATION="${AUDIT_GATE_RUNTIME_DURATION_SEC:-60}"
  RUNTIME_TICK="${AUDIT_GATE_RUNTIME_TICK_SEC:-2}"
  RUNTIME_SAMPLES="${AUDIT_GATE_RUNTIME_SAMPLE_COUNT:-20}"
  run_step bounded_shadow_runtime "$PY" tool/runtime/run_bounded_paper_shadow_runtime.py \
    --mode SHADOW_ONLY \
    --duration-sec "$RUNTIME_DURATION" \
    --tick-sec "$RUNTIME_TICK" \
    --sample-count "$RUNTIME_SAMPLES" \
    --day "$DAY" \
    --output-root "$ROOT_DIR" || FAILED=1
  run_step bounded_paper_runtime "$PY" tool/runtime/run_bounded_paper_shadow_runtime.py \
    --mode PAPER_ONLY \
    --duration-sec "$RUNTIME_DURATION" \
    --tick-sec "$RUNTIME_TICK" \
    --sample-count "$RUNTIME_SAMPLES" \
    --emit-paper-broker-events true \
    --day "$DAY" \
    --output-root "$ROOT_DIR" || FAILED=1
  run_step strategy_execution_reliability_report "$PY" tool/report/build_strategy_execution_reliability_report.py --repo-root "$ROOT_DIR" --day "$DAY" || FAILED=1
  run_step approved_to_fill_conversion_report "$PY" tool/report/build_approved_to_fill_conversion_report.py --repo-root "$ROOT_DIR" --day "$DAY" || FAILED=1
  run_step vegas_dlb_edge_measurement_report "$PY" tool/report/build_vegas_dlb_edge_measurement_report.py --repo-root "$ROOT_DIR" --day "$DAY" || FAILED=1
  run_step paper_shadow_soak_gate_report "$PY" tool/report/build_paper_shadow_soak_gate_report.py --repo-root "$ROOT_DIR" --day "$DAY" || FAILED=1
fi

build_report
REPORT_RC="$?"
if [[ "$FAILED" -ne 0 && "$REPORT_RC" -eq 0 ]]; then
  exit 2
fi
exit "$REPORT_RC"
