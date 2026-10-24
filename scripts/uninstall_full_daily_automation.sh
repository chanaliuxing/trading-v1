#!/bin/bash
# =============================================================================
# One‑Click Uninstall: Full Daily Automation (macOS launchd)
# - Unloads and removes all trading launch agents installed by setup script.
# =============================================================================
set -e

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This uninstall targets macOS launchd. Detected: $(uname -s)"
  exit 1
fi

LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABELS=(
  com.trading.health.hourly
  com.trading.morning.health
  com.trading.premarket
  com.trading.market
  com.trading.evening.review
  com.trading.superwin.research
  com.trading.candidate.factory
  com.trading.ai_trader.learn
  com.trading.overnight
  com.trading.recon.daily
)

echo "Unloading launch agents..."
for L in "${LABELS[@]}"; do
  PLIST="$LAUNCH_DIR/${L}.plist"
  if launchctl list | grep -q "$L"; then
    launchctl unload "$PLIST" 2>/dev/null || true
    echo "  - Unloaded $L"
  fi
done

echo "Removing plist files..."
for L in "${LABELS[@]}"; do
  PLIST="$LAUNCH_DIR/${L}.plist"
  if [[ -f "$PLIST" ]]; then
    rm -f "$PLIST"
    echo "  - Removed $PLIST"
  fi
done

echo ""
echo "✓ All automation launch agents removed."
echo "You can re‑enable anytime via: Start_Automation.command"
