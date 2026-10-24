#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This uninstall script only supports macOS launchd."
  exit 1
fi

LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABELS=(
  com.trading.isolation.monitor.intraday
  com.trading.isolation.summary.postclose
)

for label in "${LABELS[@]}"; do
  plist="$LAUNCH_DIR/${label}.plist"
  launchctl unload "$plist" >/dev/null 2>&1 || true
  rm -f "$plist"
  echo "Removed $label"
done

echo "Isolation monitor launchd jobs removed."
