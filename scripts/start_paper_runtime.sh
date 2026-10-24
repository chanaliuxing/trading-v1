#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="${REPO_DIR:-$REPO_DIR_DEFAULT}"
PY="${TRADING_RUNTIME_PYTHON:-$REPO_DIR/venv_ml/bin/python}"
ENTRY="$REPO_DIR/run_system.py"
LOG_DIR="${LOG_DIR:-$REPO_DIR/logs}"
LOCK_PATH="$REPO_DIR/runtime/locks/run_system.lock"
CIRCUIT_PATH="$REPO_DIR/runtime/state/ib_gateway_connection_circuit.json"
CHECK_ONLY=0

if [ "${1:-}" = "--check-only" ]; then
  CHECK_ONLY=1
fi

json_escape() {
  local input="$1"
  local output
  output="$("$PY" - "$input" <<'PY' 2>/dev/null || true
import json
import sys
print(json.dumps(sys.argv[1])[1:-1], end="")
PY
)"
  if [ -n "$output" ] || [ -z "$input" ]; then
    printf '%s' "$output"
  else
    printf '%s' "$input" | sed 's/\\/\\\\/g; s/"/\\"/g'
  fi
}

emit_json() {
  local status="$1"
  local reason="$2"
  local pid="${3:-}"
  local details="${4:-}"
  local escaped_reason escaped_details
  escaped_reason="$(json_escape "$reason")"
  escaped_details="$(json_escape "$details")"
  printf '{"status":"%s","reason":"%s","pid":"%s","details":"%s"}\n' "$status" "$escaped_reason" "$pid" "$escaped_details"
}

fail() {
  emit_json "fail" "$1" "" "${2:-}"
  echo "start_paper_runtime: $1${2:+ - $2}" >&2
  exit 1
}

cd "$REPO_DIR"
mkdir -p "$LOG_DIR" "$(dirname "$LOCK_PATH")"

if [ ! -x "$PY" ]; then
  fail "runtime_python_missing_or_not_executable" "$PY"
fi

if [ ! -f "$ENTRY" ]; then
  fail "run_system_entry_missing" "$ENTRY"
fi

if [ "${START_PAPER_RUNTIME_SKIP_DEP_CHECK:-0}" != "1" ]; then
  set +e
  "$PY" - <<'PY' >/dev/null
import importlib
for name in ("dotenv", "ib_insync"):
    importlib.import_module(name)
import run_system  # noqa: F401
PY
  dep_rc=$?
  set -e
  if [ "$dep_rc" -ne 0 ]; then
    fail "runtime_python_dependency_check_failed" "$PY"
  fi
fi

RUN_SYSTEM_PATTERN="[p]ython.*run_system.py"
existing_pid=""
if command -v pgrep >/dev/null 2>&1; then
  existing_pid="$(pgrep -f "$RUN_SYSTEM_PATTERN" | head -n 1 || true)"
else
  existing_pid="$(ps aux 2>/dev/null | awk '/[p]ython.*run_system.py/ {print $2; exit}' || true)"
fi
if [ -n "$existing_pid" ]; then
  emit_json "already_running" "run_system_process_exists" "$existing_pid" "$REPO_DIR"
  exit 0
fi

if [ -e "$LOCK_PATH" ]; then
  lock_pid="$(tr -cd '0-9' < "$LOCK_PATH" 2>/dev/null || true)"
  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    fail "run_system_lock_owned_by_live_process" "lock=$LOCK_PATH pid=$lock_pid"
  fi
  rm -f "$LOCK_PATH"
fi

if [ -f "$CIRCUIT_PATH" ] && grep -Eq '"manual_required"[[:space:]]*:[[:space:]]*true' "$CIRCUIT_PATH"; then
  if grep -Eq '"source"[[:space:]]*:[[:space:]]*"[^"]*:pre_connect_api_probe"' "$CIRCUIT_PATH"; then
    echo "start_paper_runtime: stale pre-connect probe manual circuit will be cleared by run_system" >&2
  else
  circuit_diag="$("$PY" - "$CIRCUIT_PATH" "${IB_PORT:-4002}" <<'PY' 2>/dev/null || true
import json
import subprocess
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = int(sys.argv[2])
try:
    circuit = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    circuit = {}
reason = str(circuit.get("reason") or "unknown")
last_proof_reason = str(circuit.get("last_proof_reason") or "")
port_listening = False
try:
    out = subprocess.check_output(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=2.0,
    )
    port_listening = f":{port} " in out or f"*:{port}" in out
except Exception:
    port_listening = False
status_reason = "ib_gateway_manual_required"
if port_listening and "TimeoutError" in last_proof_reason:
    status_reason = "ib_api_handshake_failed_manual_required"
print(json.dumps({
    "status_reason": status_reason,
    "reason": reason,
    "last_proof_reason": last_proof_reason,
    "ib_port_ready": port_listening,
    "ib_port_check_mode": "passive_lsof",
}, separators=(",", ":")))
PY
)"
  status_reason="ib_gateway_manual_required"
  circuit_reason="unknown"
  last_proof_reason=""
  ib_port_ready="unknown"
  if [ -n "$circuit_diag" ]; then
    status_reason="$("$PY" - "$circuit_diag" <<'PY' 2>/dev/null || printf 'ib_gateway_manual_required'
import json, sys
print(json.loads(sys.argv[1]).get("status_reason") or "ib_gateway_manual_required", end="")
PY
)"
    circuit_reason="$("$PY" - "$circuit_diag" <<'PY' 2>/dev/null || printf 'unknown'
import json, sys
print(json.loads(sys.argv[1]).get("reason") or "unknown", end="")
PY
)"
    last_proof_reason="$("$PY" - "$circuit_diag" <<'PY' 2>/dev/null || true
import json, sys
print(json.loads(sys.argv[1]).get("last_proof_reason") or "", end="")
PY
)"
    ib_port_ready="$("$PY" - "$circuit_diag" <<'PY' 2>/dev/null || printf 'unknown'
import json, sys
print(str(bool(json.loads(sys.argv[1]).get("ib_port_ready"))).lower(), end="")
PY
)"
  fi
  fail \
    "$status_reason" \
    "do_not_restart_gateway=true action=operator_verify_gateway_login_without_restart_then_run_authorized_recovery_proof circuit=$CIRCUIT_PATH reason=${circuit_reason:-unknown} last_proof_reason=${last_proof_reason:-unknown} ib_port_ready=$ib_port_ready ib_port_check_mode=passive_lsof"
  fi
fi

if [ -f "$CIRCUIT_PATH" ]; then
  nonmanual_handshake_diag="$("$PY" - "$CIRCUIT_PATH" "${IB_PORT:-4002}" <<'PY' 2>/dev/null || true
import json
import subprocess
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = int(sys.argv[2])
try:
    circuit = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    circuit = {}
if bool(circuit.get("manual_required")):
    raise SystemExit(0)
source = str(circuit.get("source") or "")
if ":pre_connect_api_probe" in source:
    raise SystemExit(0)
reason = " ".join(
    str(item or "")
    for item in (
        circuit.get("state"),
        circuit.get("reason"),
        circuit.get("last_proof_reason"),
    )
)
last_proof_status = str(circuit.get("last_proof_status") or "")
consecutive_failures = int(circuit.get("consecutive_failures") or 0)
if "TimeoutError" not in reason:
    raise SystemExit(0)
if last_proof_status != "PROOF_FAILED" and consecutive_failures < 3:
    raise SystemExit(0)
port_listening = False
try:
    out = subprocess.check_output(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=2.0,
    )
    port_listening = f":{port} " in out or f"*:{port}" in out
except Exception:
    port_listening = False
print(json.dumps({
    "reason": str(circuit.get("reason") or "unknown"),
    "last_proof_reason": str(circuit.get("last_proof_reason") or ""),
    "last_proof_status": last_proof_status,
    "consecutive_failures": consecutive_failures,
    "ib_port_ready": port_listening,
    "ib_port_check_mode": "passive_lsof",
}, separators=(",", ":")))
PY
)"
  if [ -n "$nonmanual_handshake_diag" ]; then
    circuit_reason="$("$PY" - "$nonmanual_handshake_diag" <<'PY' 2>/dev/null || printf 'unknown'
import json, sys
print(json.loads(sys.argv[1]).get("reason") or "unknown", end="")
PY
)"
    last_proof_reason="$("$PY" - "$nonmanual_handshake_diag" <<'PY' 2>/dev/null || true
import json, sys
print(json.loads(sys.argv[1]).get("last_proof_reason") or "", end="")
PY
)"
    last_proof_status="$("$PY" - "$nonmanual_handshake_diag" <<'PY' 2>/dev/null || true
import json, sys
print(json.loads(sys.argv[1]).get("last_proof_status") or "", end="")
PY
)"
    consecutive_failures="$("$PY" - "$nonmanual_handshake_diag" <<'PY' 2>/dev/null || printf '0'
import json, sys
print(json.loads(sys.argv[1]).get("consecutive_failures") or 0, end="")
PY
)"
    ib_port_ready="$("$PY" - "$nonmanual_handshake_diag" <<'PY' 2>/dev/null || printf 'unknown'
import json, sys
print(str(bool(json.loads(sys.argv[1]).get("ib_port_ready"))).lower(), end="")
PY
)"
    fail \
      "ib_api_handshake_failed_recovery_required" \
      "do_not_start_runtime=true do_not_restart_gateway=true action=operator_verify_gateway_login_without_restart_then_run_authorized_recovery_proof circuit=$CIRCUIT_PATH reason=${circuit_reason:-unknown} last_proof_status=${last_proof_status:-unknown} last_proof_reason=${last_proof_reason:-unknown} consecutive_failures=${consecutive_failures:-0} ib_port_ready=$ib_port_ready ib_port_check_mode=passive_lsof"
  fi
fi

if [ -z "${TRADING_MODE:-}" ]; then
  ENV_TRADING_MODE=""
  if [ -f "$REPO_DIR/.env" ]; then
    ENV_TRADING_MODE="$(
      awk -F= '
        /^[[:space:]]*TRADING_MODE[[:space:]]*=/ {
          value=$0
          sub(/^[[:space:]]*TRADING_MODE[[:space:]]*=[[:space:]]*/, "", value)
          sub(/[[:space:]]*#.*/, "", value)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
          gsub(/^["'\'']|["'\'']$/, "", value)
          print value
          exit
        }
      ' "$REPO_DIR/.env"
    )"
  fi
  export TRADING_MODE="${ENV_TRADING_MODE:-paper}"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  emit_json "ok" "preflight_passed" "" "python=$PY trading_mode=$TRADING_MODE"
  exit 0
fi

exec "$PY" "$ENTRY" >>"$LOG_DIR/launchd_run_system.out.log" 2>>"$LOG_DIR/launchd_run_system.err.log"
