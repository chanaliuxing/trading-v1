#!/bin/bash
#
# Daily Data Quality Check - RTH coverage monitoring
#
# Schedule: Every day at 9:00 AM (after market open)
# Crontab entry:
#   0 9 * * 1-5 ./infra/cron/data_quality_check_daily.sh >> ./logs/data_quality.log 2>&1

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Log with timestamp
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

log "=== Daily Data Quality Check Started ==="

# Check last 7 days for all active symbols
SYMBOLS="TSLA AAPL NVDA GOOGL AMZN MSFT"

log "Checking symbols: $SYMBOLS"

PYTHONPATH="$PROJECT_ROOT" "$PROJECT_ROOT/venv/bin/python" \
    "$PROJECT_ROOT/tools/data_quality_monitor.py" \
    --symbols $SYMBOLS \
    --days 7 \
    --rth-only \
    --alert-threshold 0.1 \
    --output "$PROJECT_ROOT/runs/ai_trader/$(date +%Y-%m-%d)/data_quality.json"

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log "=== Daily Data Quality Check Completed ==="
else
    log "=== Daily Data Quality Check Failed (exit code: $EXIT_CODE) ==="
fi

exit $EXIT_CODE
