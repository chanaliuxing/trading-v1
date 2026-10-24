#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This setup script only supports macOS launchd."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
TEMPLATE_DIR="$PROJECT_ROOT/ops/launchd"
LOG_DIR="$PROJECT_ROOT/logs"

mkdir -p "$LAUNCH_DIR" "$LOG_DIR"

render_plist() {
  local template="$1"
  local out="$2"
  local program="$3"
  local log_out="$4"
  local log_err="$5"
  sed \
    -e "s|__PROGRAM__|$program|g" \
    -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__PATH__|$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin|g" \
    -e "s|__LOG_OUT__|$log_out|g" \
    -e "s|__LOG_ERR__|$log_err|g" \
    "$template" > "$out"
}

install_job() {
  local label="$1"
  local template="$2"
  local program="$3"
  local log_prefix="$4"
  local plist="$LAUNCH_DIR/${label}.plist"
  render_plist \
    "$template" \
    "$plist" \
    "$program" \
    "$LOG_DIR/${log_prefix}.log" \
    "$LOG_DIR/${log_prefix}_error.log"
  launchctl unload "$plist" >/dev/null 2>&1 || true
  launchctl load "$plist"
  echo "Installed $label"
}

install_job \
  "com.trading.rollout.preopen.smoke" \
  "$TEMPLATE_DIR/com.trading.rollout.preopen.smoke.plist" \
  "$PROJECT_ROOT/scripts/automation/rollout_preopen_smoke.sh" \
  "rollout_preopen_smoke"

install_job \
  "com.trading.opening.supervisor.preopen" \
  "$TEMPLATE_DIR/com.trading.opening.supervisor.preopen.plist" \
  "$PROJECT_ROOT/scripts/automation/opening_supervisor.sh" \
  "opening_supervisor_preopen"

install_job \
  "com.trading.opening.supervisor.open" \
  "$TEMPLATE_DIR/com.trading.opening.supervisor.open.plist" \
  "$PROJECT_ROOT/scripts/automation/opening_supervisor.sh" \
  "opening_supervisor_open"

install_job \
  "com.trading.opening.supervisor.acceptance" \
  "$TEMPLATE_DIR/com.trading.opening.supervisor.acceptance.plist" \
  "$PROJECT_ROOT/scripts/automation/opening_supervisor.sh" \
  "opening_supervisor_acceptance"

install_job \
  "com.trading.runtime.watchdog" \
  "$TEMPLATE_DIR/com.trading.runtime.watchdog.plist" \
  "$PROJECT_ROOT/scripts/automation/runtime_watchdog.sh" \
  "runtime_watchdog"

install_job \
  "com.trading.rollout.postclose.evidence" \
  "$TEMPLATE_DIR/com.trading.rollout.postclose.evidence.plist" \
  "$PROJECT_ROOT/scripts/automation/postclose_rollout_evidence.sh" \
  "rollout_postclose_evidence"

echo
echo "Rollout ops launchd jobs installed."
echo "  Preopen smoke: 09:20 local time daily"
echo "  Preopen supervisor: 09:25 local time daily"
echo "  Opening supervisor: 09:32 local time daily"
echo "  Acceptance supervisor: 09:40 local time daily"
echo "  Runtime watchdog: every 300 seconds"
echo "  Postclose evidence: 16:20 local time daily"
echo "Status:"
echo "  launchctl list | grep 'com.trading\\.(rollout|opening|runtime.watchdog)'"
