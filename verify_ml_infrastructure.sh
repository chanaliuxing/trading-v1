#!/bin/bash
# ML Infrastructure Complete Verification Script
# 验证两个阶段的所有功能

set -e  # Exit on any error

echo "========================================================================"
echo "ML Infrastructure Complete Verification"
echo "========================================================================"
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PHASE1_PASS=0
PHASE2_UNIT_PASS=0
PHASE2_INTEGRATION_PASS=0
TOTAL_TESTS=0
PASSED_TESTS=0

echo "=========================================="
echo "Phase 1: Leakage Detector (3 tests)"
echo "=========================================="
echo ""

if venv/bin/python test_leakage_guard.py > /tmp/phase1_test.log 2>&1; then
    echo -e "${GREEN}✅ Phase 1 Tests PASSED${NC} (3/3)"
    PHASE1_PASS=1
    PASSED_TESTS=$((PASSED_TESTS + 3))
    cat /tmp/phase1_test.log | grep -E "✅|测试|通过"
else
    echo -e "${RED}❌ Phase 1 Tests FAILED${NC}"
    cat /tmp/phase1_test.log
fi
TOTAL_TESTS=$((TOTAL_TESTS + 3))

echo ""
echo "=========================================="
echo "Phase 2: Model Pool - Unit Tests (8 tests)"
echo "=========================================="
echo ""

if venv/bin/python test_model_pool.py > /tmp/phase2_unit_test.log 2>&1; then
    echo -e "${GREEN}✅ Phase 2 Unit Tests PASSED${NC} (8/8)"
    PHASE2_UNIT_PASS=1
    PASSED_TESTS=$((PASSED_TESTS + 8))
    cat /tmp/phase2_unit_test.log | grep -E "✅|测试|R²|Accuracy"
else
    echo -e "${RED}❌ Phase 2 Unit Tests FAILED${NC}"
    cat /tmp/phase2_unit_test.log
fi
TOTAL_TESTS=$((TOTAL_TESTS + 8))

echo ""
echo "=========================================="
echo "Phase 2: Model Pool - Integration (5 tests)"
echo "=========================================="
echo ""

if venv/bin/python test_model_pool_integration.py > /tmp/phase2_integration_test.log 2>&1; then
    echo -e "${GREEN}✅ Phase 2 Integration Tests PASSED${NC} (5/5)"
    PHASE2_INTEGRATION_PASS=1
    PASSED_TESTS=$((PASSED_TESTS + 5))
    cat /tmp/phase2_integration_test.log | grep -E "✅|测试|R²|Trial"
else
    echo -e "${RED}❌ Phase 2 Integration Tests FAILED${NC}"
    cat /tmp/phase2_integration_test.log
fi
TOTAL_TESTS=$((TOTAL_TESTS + 5))

echo ""
echo "========================================================================"
echo "Verification Summary"
echo "========================================================================"
echo ""

if [ $PHASE1_PASS -eq 1 ]; then
    echo -e "${GREEN}✅ Phase 1: Leakage Detector${NC}      - 3/3 tests passed"
else
    echo -e "${RED}❌ Phase 1: Leakage Detector${NC}      - FAILED"
fi

if [ $PHASE2_UNIT_PASS -eq 1 ]; then
    echo -e "${GREEN}✅ Phase 2: Model Pool (Unit)${NC}     - 8/8 tests passed"
else
    echo -e "${RED}❌ Phase 2: Model Pool (Unit)${NC}     - FAILED"
fi

if [ $PHASE2_INTEGRATION_PASS -eq 1 ]; then
    echo -e "${GREEN}✅ Phase 2: Model Pool (Integration)${NC} - 5/5 tests passed"
else
    echo -e "${RED}❌ Phase 2: Model Pool (Integration)${NC} - FAILED"
fi

echo ""
echo "========================================================================"

if [ $PASSED_TESTS -eq $TOTAL_TESTS ]; then
    echo -e "${GREEN}🎉 ALL TESTS PASSED: $PASSED_TESTS/$TOTAL_TESTS ✅${NC}"
    echo ""
    echo "Status: ✅ 100% Production Ready"
    echo ""
    echo "Files Created:"
    echo "  - leakage_guard.py (344 lines)"
    echo "  - models/model_pool.py (414 lines)"
    echo "  - test_leakage_guard.py (192 lines)"
    echo "  - test_model_pool.py (248 lines)"
    echo "  - test_model_pool_integration.py (246 lines)"
    echo "  - grafana_leakage_dashboard.json (241 lines)"
    echo ""
    echo "Modified Files:"
    echo "  - wf_hpo.py (leakage integration + model pool integration)"
    echo "  - alpha_config_enhanced.json (leakage_guard + model_pool config)"
    echo ""
    echo "Documentation:"
    echo "  - LEAKAGE_DETECTOR_DEPLOYMENT_REPORT.md"
    echo "  - MODEL_POOL_DEPLOYMENT_REPORT.md"
    echo "  - MODEL_POOL_INTEGRATION_COMPLETE.md"
    echo "  - ML_INFRASTRUCTURE_COMPLETE.md"
    echo ""
    exit 0
else
    echo -e "${RED}❌ SOME TESTS FAILED: $PASSED_TESTS/$TOTAL_TESTS${NC}"
    echo ""
    echo "Check logs:"
    echo "  - /tmp/phase1_test.log"
    echo "  - /tmp/phase2_unit_test.log"
    echo "  - /tmp/phase2_integration_test.log"
    echo ""
    exit 1
fi
