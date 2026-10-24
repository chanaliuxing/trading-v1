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
  "com.trading.real_session.shadow_capture.intraday" \
  "$TEMPLATE_DIR/com.trading.real_session.shadow_capture.intraday.plist" \
  "$PROJECT_ROOT/scripts/automation/real_session_shadow_capture.sh" \
  "real_session_shadow_capture"

install_job \
  "com.trading.real_session.postclose.bundle" \
  "$TEMPLATE_DIR/com.trading.real_session.postclose.bundle.plist" \
  "$PROJECT_ROOT/scripts/automation/real_session_postclose_bundle.sh" \
  "real_session_postclose_bundle"

legacy_labels=(
  "com.trading.real_session.proof.postclose"
  "com.trading.real_session.proof.summary.postclose"
)
for label in "${legacy_labels[@]}"; do
  plist="$LAUNCH_DIR/${label}.plist"
  launchctl unload "$plist" >/dev/null 2>&1 || true
  rm -f "$plist"
done

echo
echo "Real-session proof launchd jobs installed."
echo "  Shadow capture: 09:35 local time daily"
echo "  Postclose bundle: 16:12 local time daily"
echo "Logs:"
echo "  $LOG_DIR/real_session_shadow_capture.log"
echo "  $LOG_DIR/real_session_shadow_capture_error.log"
echo "  $LOG_DIR/real_session_postclose_bundle.log"
echo "  $LOG_DIR/real_session_postclose_bundle_error.log"
echo "Status:"
echo "  launchctl list | grep 'com.trading.real_session'"
