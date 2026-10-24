#!/bin/bash
#
# Daily Active Table Sync - Runs after daily data pipeline
#
# Schedule: Every day at 4:00 AM (after daily_data_pipeline.sh)
# Crontab entry:
#   0 4 * * * ./infra/cron/sync_features_active_daily.sh >> ./logs/sync_active.log 2>&1

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Log with timestamp
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

log "=== Daily Active Table Sync Started ==="

# Sync yesterday's data for all active symbols
SYMBOLS="TSLA,AAPL,NVDA,GOOGL,AMZN,MSFT"

log "Syncing symbols: $SYMBOLS"

PYTHONPATH="$PROJECT_ROOT" "$PROJECT_ROOT/venv/bin/python" \
    "$PROJECT_ROOT/tools/sync_features_active.py" \
    --symbols "$SYMBOLS" \
    --yesterday \
    --validate \
    --batch-days 1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log "=== Daily Active Table Sync Completed Successfully ==="
else
    log "=== Daily Active Table Sync Failed (exit code: $EXIT_CODE) ==="
fi

exit $EXIT_CODE
