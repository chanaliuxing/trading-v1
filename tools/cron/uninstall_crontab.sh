#!/bin/bash
# Uninstall P3-P4 Crontab

set -e

echo "Uninstalling P3-P4 cron jobs..."

# Create temporary crontab file
TEMP_CRON=$(mktemp)

# Get existing crontab
crontab -l > "$TEMP_CRON" 2>/dev/null || true

# Remove P3-P4 entries
sed -i.bak '/# P3-P4 Components/,/# End P3-P4 Components/d' "$TEMP_CRON"

# Install updated crontab
crontab "$TEMP_CRON"

# Clean up
rm "$TEMP_CRON" "$TEMP_CRON.bak" 2>/dev/null || true

echo "✅ P3-P4 cron jobs uninstalled successfully!"

exit 0
