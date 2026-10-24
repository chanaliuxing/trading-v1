#!/bin/bash
#
# Time Verification Pre-Trading Check
# 在启动交易前必须运行此脚本验证时间正确性
#
# Usage:
#   ./scripts/ai_trader/tools/verify_time_before_trading.sh
#
# Exit codes:
#   0 - All checks passed, safe to trade
#   1 - Critical time errors, DO NOT trade
#   2 - Warnings present, review before trading
#

set -e

PROJECT_ROOT="."
VERIFICATION_SCRIPT="$PROJECT_ROOT/scripts/ai_trader/tools/time_verification.py"
RESULTS_FILE="/tmp/ai_trader_time_check_$(date +%Y%m%d_%H%M%S).json"

echo "========================================"
echo "AI Trader Time Verification"
echo "========================================"
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""

# Check if verification script exists
if [ ! -f "$VERIFICATION_SCRIPT" ]; then
    echo "❌ ERROR: Verification script not found at $VERIFICATION_SCRIPT"
    exit 1
fi

# Make script executable
chmod +x "$VERIFICATION_SCRIPT"

echo "Running comprehensive time verification..."
echo ""

# Run full verification
cd "$PROJECT_ROOT"
PYTHONPATH="$PROJECT_ROOT" python "$VERIFICATION_SCRIPT" --full --output "$RESULTS_FILE"
RESULT=$?

echo ""
echo "========================================"

if [ $RESULT -eq 0 ]; then
    echo "✅ ALL TIME CHECKS PASSED"
    echo ""
    echo "Results saved to: $RESULTS_FILE"
    echo ""
    echo "Summary:"
    python3 -c "import json; data=json.load(open('$RESULTS_FILE')); print(f\"  Checks passed: {data['checks_passed']}/{data['checks_total']}\"); print(f\"  Errors: {len(data['errors'])}\"); print(f\"  Overall: {'✅ HEALTHY' if data['overall_ok'] else '❌ ERROR'}\")"
    echo ""
    echo "🟢 SAFE TO START TRADING"
    exit 0
else
    echo "❌ TIME VERIFICATION FAILED"
    echo ""
    echo "Results saved to: $RESULTS_FILE"
    echo ""
    echo "Summary:"
    python3 -c "import json; data=json.load(open('$RESULTS_FILE')); print(f\"  Checks passed: {data['checks_passed']}/{data['checks_total']}\"); print(f\"  Errors: {len(data['errors'])}\"); [print(f\"    - {e}\") for e in data['errors']]; print(f\"  Overall: {'✅ HEALTHY' if data['overall_ok'] else '❌ ERROR'}\")"
    echo ""
    echo "🔴 DO NOT START TRADING - Fix time issues first"
    echo ""
    echo "Review the detailed results:"
    echo "  cat $RESULTS_FILE | python3 -m json.tool"
    exit 1
fi
