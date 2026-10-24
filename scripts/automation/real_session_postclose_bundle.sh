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
    echo "[SKIP] Weekend real-session postclose bundle skipped."
    exit 0
  fi
fi

echo "[1/6] preflight export"
bash "$PROJECT_ROOT/scripts/automation/real_session_preflight_export.sh"

echo "[2/6] trace smoke gate"
set +e
bash "$PROJECT_ROOT/scripts/automation/real_session_trace_smoke_gate.sh"
trace_smoke_rc=$?
set -e
if [[ "$trace_smoke_rc" -ne 0 && "$trace_smoke_rc" -ne 1 ]]; then
  echo "[FAIL] real_session_trace_smoke_gate.sh exited with rc=$trace_smoke_rc"
  exit "$trace_smoke_rc"
fi

echo "[3/6] proof pipeline"
set +e
bash "$PROJECT_ROOT/scripts/automation/real_session_proof_pipeline.sh"
proof_rc=$?
set -e
if [[ "$proof_rc" -ne 0 && "$proof_rc" -ne 1 ]]; then
  echo "[FAIL] real_session_proof_pipeline.sh exited with rc=$proof_rc"
  exit "$proof_rc"
fi

echo "[4/6] proof daily summary"
bash "$PROJECT_ROOT/scripts/automation/real_session_proof_daily_summary.sh"

echo "[5/6] isolation daily summary"
bash "$PROJECT_ROOT/scripts/automation/isolation_daily_summary.sh"

echo "[6/6] final enablement checklist"
set +e
bash "$PROJECT_ROOT/scripts/automation/real_session_enablement_checklist.sh"
checklist_rc=$?
set -e
if [[ "$checklist_rc" -ne 0 && "$checklist_rc" -ne 1 ]]; then
  echo "[FAIL] real_session_enablement_checklist.sh exited with rc=$checklist_rc"
  exit "$checklist_rc"
fi

if [[ "$trace_smoke_rc" -eq 1 || "$proof_rc" -eq 1 || "$checklist_rc" -eq 1 ]]; then
  echo "[FAIL] Postclose bundle completed with non-green evidence state."
  exit 1
fi

echo "[DONE] Postclose bundle completed green."
