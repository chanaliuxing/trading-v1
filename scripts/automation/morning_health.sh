#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "================================================================"
echo "Morning Health Check - Enhanced with Data Integrity"
echo "================================================================"
echo "$(date)"
echo ""

# Layer A: Pre-market Data Integrity Check & Auto-Seed
echo "----------------------------------------------------------------"
echo "[System] Layer A: Pre-market Data Integrity Check & Auto-Seed"
echo "----------------------------------------------------------------"

# Define check date (today or yesterday if before market open)
HOUR=$(date +%H)
if [ "$HOUR" -lt "09" ]; then
    # Before 9 AM, check yesterday's data
    CHECK_DATE=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d "yesterday" +%Y-%m-%d)
    echo "[Info] Before market hours, checking yesterday: $CHECK_DATE"
else
    # After 9 AM, check today's data
    CHECK_DATE=$(date +%Y-%m-%d)
    echo "[Info] Market hours, checking today: $CHECK_DATE"
fi

# Define critical symbols
SYMBOLS="TSLA,NVDA,SPY,AAPL,MSFT,META,AMD,GOOGL,QQQ,AMZN"

# Run integrity check with auto-seed
echo "[Check] Running features_1m_active sync check for: $SYMBOLS"
echo ""

"$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tools/check_active_completeness.py" \
    --date "$CHECK_DATE" \
    --symbols "$SYMBOLS" \
    --min-coverage 0.98 \
    --auto-seed-active

# Capture exit code
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "[Success] ✅ Data Integrity Check Passed"
    echo "         Features Active Table is synced (≥98% coverage)"
else
    echo "[CRITICAL] ❌ Data Integrity Check Failed even after auto-seed!"
    echo ""
    echo "Possible causes:"
    echo "  1. Source table (features_1m) has no data for this date"
    echo "  2. bars_1m_ibkr has no bars (IBKR not running)"
    echo "  3. ClickHouse connection failed"
    echo ""
    echo "[Action] Stopping startup sequence to prevent trading on bad data"
    echo ""

    # Optional: Send alert (uncomment if you have alert script)
    # "$SCRIPT_DIR/send_alert.sh" "CRITICAL: Morning Health Check Failed" || true

    exit 1
fi

echo ""
echo "----------------------------------------------------------------"
echo "[System] Layer A Complete - Proceeding to Standard Checks"
echo "----------------------------------------------------------------"
echo ""

# Original health check
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/docs/daily"
./morning_check.sh || true

echo ""
echo "================================================================"
echo "Morning Health Check Complete ✅"
echo "================================================================"
