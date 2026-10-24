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
  "com.trading.isolation.monitor.intraday" \
  "$TEMPLATE_DIR/com.trading.isolation.monitor.intraday.plist" \
  "$PROJECT_ROOT/scripts/automation/isolation_monitor.sh" \
  "isolation_monitor"

install_job \
  "com.trading.isolation.summary.postclose" \
  "$TEMPLATE_DIR/com.trading.isolation.summary.postclose.plist" \
  "$PROJECT_ROOT/scripts/automation/isolation_daily_summary.sh" \
  "isolation_daily_summary"

echo
echo "Isolation monitor launchd jobs installed."
echo "  Intraday monitor: 09:31 local time daily"
echo "  Post-close summary: 16:10 local time daily"
echo "Logs:"
echo "  $LOG_DIR/isolation_monitor.log"
echo "  $LOG_DIR/isolation_monitor_error.log"
echo "  $LOG_DIR/isolation_daily_summary.log"
echo "  $LOG_DIR/isolation_daily_summary_error.log"
echo "Status:"
echo "  launchctl list | grep 'com.trading.isolation'"
