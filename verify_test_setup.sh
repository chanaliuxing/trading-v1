#!/bin/bash
# Quick verification that test suite is ready

echo "=========================================="
echo "  Test Suite Setup Verification"
echo "=========================================="
echo ""

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

check() {
    if [ $1 -eq 0 ]; then
        echo -e "${GREEN}✓${NC} $2"
    else
        echo -e "${RED}✗${NC} $2"
    fi
}

# Check files exist
[ -f "test_rth_suite.sh" ]; check $? "test_rth_suite.sh exists"
[ -x "test_rth_suite.sh" ]; check $? "test_rth_suite.sh is executable"
[ -f "RTH_TEST_GUIDE.md" ]; check $? "RTH_TEST_GUIDE.md exists"
[ -f "QUICK_RTH_TEST.md" ]; check $? "QUICK_RTH_TEST.md exists"
[ -f "TEST_RESULTS_SUMMARY.md" ]; check $? "TEST_RESULTS_SUMMARY.md exists"

# Check infrastructure
docker ps | grep -q clickhouse; check $? "ClickHouse running"
docker ps | grep -q kafka; check $? "Kafka running"
docker ps | grep -q redis; check $? "Redis running"

# Check processes
pgrep -f "auto_loop.py" > /dev/null; check $? "auto_loop.py running"
pgrep -f "ml_scheduler.py" > /dev/null; check $? "ml_scheduler.py running"
pgrep -f "control_event_consumer.py" > /dev/null; check $? "control_event_consumer.py running"

# Check config
grep -q "use_rfe_active_table: true" config.yml; check $? "RFE active table enabled"
grep -q "TRADING_MODE=paper" .env; check $? "Paper mode enabled"

echo ""
echo "=========================================="
echo "  Setup Status"
echo "=========================================="
echo ""
echo "All checks complete. If any items failed:"
echo "  - Infrastructure: Run 'make up' in infra/"
echo "  - Processes: Check with 'ps aux | grep -E \"auto_loop|ml_scheduler\"'"
echo "  - Config: Review config.yml and .env"
echo ""
echo "When ready, run RTH tests during market hours:"
echo "  ./test_rth_suite.sh"
echo ""
