#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 1. IMMEDIATE PROCESS CHECK (Zero-disk-op start)
# Prefer pgrep to avoid sandboxed ps failures; fall back only if needed. The
# bracketed regex avoids matching this wrapper command itself.
RUN_SYSTEM_PATTERN="[p]ython.*run_system.py"
if command -v pgrep >/dev/null 2>&1; then
  if pgrep -f "$RUN_SYSTEM_PATTERN" >/dev/null 2>&1; then
    exit 0
  fi
elif ps aux 2>/dev/null | grep -E "$RUN_SYSTEM_PATTERN" >/dev/null 2>&1; then
  exit 0
fi

USER_HOME="${HOME:-$(eval echo ~$(id -un))}"
REPO_DIR="${REPO_DIR:-$REPO_DIR_DEFAULT}"
PY="${TRADING_RUNTIME_PYTHON:-$REPO_DIR/venv_ml/bin/python}"
START_SCRIPT="$REPO_DIR/scripts/start_paper_runtime.sh"
LOG_DIR="${LOG_DIR:-$REPO_DIR/logs}"

cd "$REPO_DIR"
mkdir -p "$LOG_DIR"

_audit_log_path_usable() {
  local target="$1"
  local parent
  parent="$(dirname "$target")"
  mkdir -p "$parent" 2>/dev/null || return 1
  [ -w "$parent" ] || return 1
  "$PY" - "$target" <<'PY' >/dev/null 2>&1
import logging.handlers
import sys

target = sys.argv[1]
handler = logging.handlers.RotatingFileHandler(target, maxBytes=1, backupCount=1)
handler.close()
PY
}

# Prefer the Grafana/Promtail path, but keep launch fallback usable in
# restricted automation contexts where the home observability stack is read-only.
if [ -z "${AUDIT_LOG_PATH:-}" ]; then
  GRAFANA_LOG_DIR="${GRAFANA_LOG_DIR:-$USER_HOME/grafana-stack/logs}"
  if _audit_log_path_usable "$GRAFANA_LOG_DIR/trader.log"; then
    export AUDIT_LOG_PATH="$GRAFANA_LOG_DIR/trader.log"
  else
    export AUDIT_LOG_PATH="$LOG_DIR/trader.log"
  fi
elif ! _audit_log_path_usable "$AUDIT_LOG_PATH"; then
  export AUDIT_LOG_PATH="$LOG_DIR/trader.log"
fi

# Ensure a safe default without overriding .env. python-dotenv does not replace
# existing environment variables, so exporting "shadow" here would mask
# TRADING_MODE=paper/live configured in .env.
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
  export TRADING_MODE="${ENV_TRADING_MODE:-shadow}"
fi

exec "$START_SCRIPT" >>"$LOG_DIR/launchd_run_system.out.log" 2>>"$LOG_DIR/launchd_run_system.err.log"
