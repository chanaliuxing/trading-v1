#!/bin/bash
# Double-click this file to uninstall/stop all daily automation (macOS)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/scripts/uninstall_full_daily_automation.sh"
echo ""
read -p "Automation stopped. Press Enter to close..." _

