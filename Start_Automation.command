#!/bin/bash
# Double-click this file to install full daily automation (macOS)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/scripts/setup_full_daily_automation.sh"
echo ""
echo "If prompted for permissions, allow Terminal to run this script."
read -p "Press Enter to close..." _

