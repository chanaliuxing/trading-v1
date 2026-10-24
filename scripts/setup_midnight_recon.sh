#!/bin/bash
##############################################################################
# Setup Midnight Daily Recon Automation (00:01 AM)
# Runs recon-all every day at 00:01 (includes data reconciliation,
# feature computation, and daily backups via Makefile target)
##############################################################################

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_NAME="com.trading.recon.midnight"
PLIST_FILE="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo -e "${GREEN}================================================================================${NC}"
echo -e "${GREEN}Trading System Midnight Recon Setup (00:01)${NC}"
echo -e "${GREEN}================================================================================${NC}"
echo ""

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJECT_ROOT/logs"

cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${PROJECT_ROOT}/scripts/run_recon_daily.sh</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>1</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${PROJECT_ROOT}/logs/recon_midnight.log</string>

    <key>StandardErrorPath</key>
    <string>${PROJECT_ROOT}/logs/recon_midnight_error.log</string>

    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

echo -e "${YELLOW}Loading launchd job...${NC}"
if launchctl list | grep -q "$PLIST_NAME"; then
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
fi
launchctl load "$PLIST_FILE"
echo -e "${GREEN}✓ Setup complete. Will run daily at 00:01.${NC}"

echo "Logs:"
echo "  $PROJECT_ROOT/logs/recon_midnight.log"
echo "  $PROJECT_ROOT/logs/recon_midnight_error.log"

