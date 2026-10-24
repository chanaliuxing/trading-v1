#!/bin/bash
#
# Daily Data Quality Check - 每日数据质量检查
#
# Usage:
#   ./scripts/daily_data_check.sh              # Check yesterday's data
#   ./scripts/daily_data_check.sh 2025-10-27   # Check specific date
#   ./scripts/daily_data_check.sh --full       # Full validation

set -e

TRADING_ROOT="."
cd "$TRADING_ROOT"

# Default settings
DATE_TO_CHECK="${1:-$(date -v-1d +%Y-%m-%d)}"
FULL_MODE=false

# Parse arguments
if [ "$1" == "--full" ] || [ "$2" == "--full" ]; then
    FULL_MODE=true
fi

# Symbols to check (can be customized)
SYMBOLS="AAPL,TSLA,NVDA,GOOGL,MSFT,META,AMZN,SPY,QQQ"

echo "=========================================="
echo "🔍 Daily Data Quality Check"
echo "=========================================="
echo "Date: $DATE_TO_CHECK"
echo "Symbols: $SYMBOLS"
echo ""

# Ensure reports directory exists
mkdir -p reports

# Run validation
if [ "$FULL_MODE" = true ]; then
    echo "Running FULL validation..."
    python tools/comprehensive_data_validation.py \
        --date "$DATE_TO_CHECK" \
        --symbols "$SYMBOLS" \
        --full \
        --output "reports/data_quality_${DATE_TO_CHECK}.json"
else
    echo "Running QUICK validation..."
    python tools/comprehensive_data_validation.py \
        --date "$DATE_TO_CHECK" \
        --symbols "$SYMBOLS" \
        --quick
fi

EXIT_CODE=$?

echo ""
echo "=========================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Data quality check PASSED"
else
    echo "⚠️  Data quality check found issues"
    echo "Review the output above for details"
    if [ -f "reports/data_quality_${DATE_TO_CHECK}.json" ]; then
        echo "Full report: reports/data_quality_${DATE_TO_CHECK}.json"
    fi
fi
echo "=========================================="

exit $EXIT_CODE
