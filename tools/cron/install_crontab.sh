#!/bin/bash
# Install Crontab for P3-P4 Components

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Installing P3-P4 cron jobs..."

# Create temporary crontab file
TEMP_CRON=$(mktemp)

# Get existing crontab (if any)
crontab -l > "$TEMP_CRON" 2>/dev/null || true

# Remove existing P3-P4 entries
sed -i.bak '/# P3-P4 Components/,/# End P3-P4 Components/d' "$TEMP_CRON"

# Add new P3-P4 entries
cat >> "$TEMP_CRON" <<EOF

# P3-P4 Components Cron Jobs
# Memory consolidation (daily at 2:00 AM)
0 2 * * * $SCRIPT_DIR/memory_consolidation.sh

# Experiment cleanup (weekly on Sunday at 3:00 AM)
0 3 * * 0 $SCRIPT_DIR/experiment_cleanup.sh

# Health check (every hour)
0 * * * * $SCRIPT_DIR/health_check.sh
# End P3-P4 Components

EOF

# Install new crontab
crontab "$TEMP_CRON"

# Clean up
rm "$TEMP_CRON" "$TEMP_CRON.bak" 2>/dev/null || true

echo "✅ Cron jobs installed successfully!"
echo ""
echo "Active cron jobs:"
crontab -l | grep -A 10 "P3-P4 Components"

exit 0
