#!/bin/bash
##############################################################################
# Setup Daily Trading System Automation
# Runs recon-all every day at 4:01 PM (includes data reconciliation,
# feature computation, and ClickHouse backup)
##############################################################################

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_NAME="com.trading.recon.daily"
PLIST_FILE="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo -e "${GREEN}================================================================================${NC}"
echo -e "${GREEN}Trading System Daily Automation Setup${NC}"
echo -e "${GREEN}================================================================================${NC}"
echo ""
echo "This script will set up automated daily tasks at 4:01 PM:"
echo "  1. Data reconciliation (all time resolutions)"
echo "  2. Feature computation (all resolutions)"
echo "  3. PostgreSQL full backup (schema + data separated)"
echo "  4. ClickHouse full backup (all tables, complete)"
echo ""

# Create LaunchAgents directory if it doesn't exist
echo -e "${YELLOW}[1/4] Creating LaunchAgents directory...${NC}"
mkdir -p "$HOME/Library/LaunchAgents"
echo -e "${GREEN}✓ Directory ready${NC}"

# Create logs directory
echo -e "\n${YELLOW}[2/4] Creating logs directory...${NC}"
mkdir -p "$PROJECT_ROOT/logs"
echo -e "${GREEN}✓ Logs directory ready${NC}"

# Create plist file
echo -e "\n${YELLOW}[3/4] Creating launchd plist...${NC}"

cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/make</string>
        <string>recon-all</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>1</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${PROJECT_ROOT}/logs/recon_daily.log</string>

    <key>StandardErrorPath</key>
    <string>${PROJECT_ROOT}/logs/recon_daily_error.log</string>

    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${PROJECT_ROOT}/venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

echo -e "${GREEN}✓ Plist created at: $PLIST_FILE${NC}"

# Load the plist
echo -e "\n${YELLOW}[4/4] Loading launchd job...${NC}"

# Unload first if already loaded
if launchctl list | grep -q "$PLIST_NAME"; then
    echo "  → Unloading existing job..."
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
fi

# Load the new job
launchctl load "$PLIST_FILE"
echo -e "${GREEN}✓ Launchd job loaded${NC}"

# Verify
echo ""
echo -e "${GREEN}================================================================================${NC}"
echo -e "${GREEN}Setup Complete!${NC}"
echo -e "${GREEN}================================================================================${NC}"
echo ""
echo "Daily Automation Configuration:"
echo "  Schedule: Every day at 4:01 PM (16:01)"
echo "  Tasks:"
echo "    1. Data Reconciliation (1m, 5m, 10m, 15m, 30m, 2h, 3h, 4h, 1d)"
echo "    2. Feature Computation (all resolutions, last 3 days)"
echo "    3. PostgreSQL Full Backup (to backup/pg/YYYY-MM-DD/)"
echo "       - Each database backed up separately"
echo "       - Schema and data split"
echo "       - Foreign key order preserved"
echo "    4. ClickHouse Full Backup (to backup/clickhouse/YYYY-MM-DD/)"
echo "       - All tables structure and data"
echo "       - Verification report included"
echo ""
echo "Log Files:"
echo "  Output: $PROJECT_ROOT/logs/recon_daily.log"
echo "  Errors: $PROJECT_ROOT/logs/recon_daily_error.log"
echo ""
echo "Launchd Job:"
echo "  Name: $PLIST_NAME"
echo "  Plist: $PLIST_FILE"
echo ""
echo -e "${YELLOW}Management Commands:${NC}"
echo "  Test now:         make recon-all"
echo "  Manual trigger:   launchctl start $PLIST_NAME"
echo "  Check status:     launchctl list | grep trading"
echo "  View logs:        tail -f $PROJECT_ROOT/logs/recon_daily.log"
echo "  View errors:      tail -f $PROJECT_ROOT/logs/recon_daily_error.log"
echo "  Unload job:       launchctl unload $PLIST_FILE"
echo "  Reload job:       launchctl unload $PLIST_FILE && launchctl load $PLIST_FILE"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "  1. Test manually: make recon-all"
echo "  2. Wait for scheduled run at 4:01 PM"
echo "  3. Check logs: tail -f logs/recon_daily.log"
echo ""
echo -e "${GREEN}================================================================================${NC}"
echo ""
echo -e "${YELLOW}Note:${NC} The first automated run will be at 4:01 PM today (or tomorrow if it's"
echo "already past 4:01 PM). You can test it immediately with 'make recon-all'."
echo ""
