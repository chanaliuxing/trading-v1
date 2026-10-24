#!/bin/bash
# Comprehensive Verification Script for P3-P4 Implementation

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "========================================"
echo "P3-P4 Implementation Verification"
echo "========================================"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass_count=0
fail_count=0

check_item() {
    local description=$1
    local command=$2

    echo -n "Checking: $description... "

    if eval "$command" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ PASS${NC}"
        ((pass_count++))
        return 0
    else
        echo -e "${RED}✗ FAIL${NC}"
        ((fail_count++))
        return 1
    fi
}

echo "=== 1. Database Schema Verification ==="
echo ""

check_item "Memory Fabric database exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS DATABASE memory_fabric'"

check_item "Evolution database exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS DATABASE evolution'"

check_item "STS table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE memory_fabric.short_term'"

check_item "MTS table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE memory_fabric.mid_term'"

check_item "LTS table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE memory_fabric.long_term'"

check_item "Promotion log table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE memory_fabric.promotion_log'"

check_item "Access log table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE memory_fabric.access_log'"

check_item "Patch approval queue table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE evolution.patch_approval_queue'"

check_item "Patch evaluation details table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE evolution.patch_evaluation_details'"

check_item "Patch comparison table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE evolution.patch_comparison'"

check_item "Patch application history table exists" \
    "docker exec trading_clickhouse clickhouse-client --query 'EXISTS TABLE evolution.patch_application_history'"

echo ""
echo "=== 2. Python Module Verification ==="
echo ""

cd "$PROJECT_ROOT"

check_item "MemoryConsolidator module" \
    "PYTHONPATH=. venv/bin/python -c 'from scripts.ai_trader.memory.memory_consolidator import MemoryConsolidator'"

check_item "PatchEvaluator module" \
    "PYTHONPATH=. venv/bin/python -c 'from tools.patch_evaluator import PatchEvaluator'"

check_item "VariantComparator module" \
    "PYTHONPATH=. venv/bin/python -c 'from libs.experiment.variant_comparator import VariantComparator'"

check_item "EvolutionStrategyManager module" \
    "PYTHONPATH=. venv/bin/python -c 'from libs.evolution.evolution_strategy_manager import EvolutionStrategyManager'"

echo ""
echo "=== 3. Integration Tests ==="
echo ""

check_item "P3 Memory Fabric tests" \
    "PYTHONPATH=. venv/bin/python -m pytest tests/integration/test_p3_memory_fabric.py -q --tb=no"

check_item "P3 Patch Evaluator tests" \
    "PYTHONPATH=. venv/bin/python -m pytest tests/integration/test_p3_patch_evaluator.py -q --tb=no"

check_item "P4 Variant Comparator tests" \
    "PYTHONPATH=. venv/bin/python -m pytest tests/integration/test_p4_variant_comparator.py -q --tb=no"

check_item "P4 Evolution Strategy tests" \
    "PYTHONPATH=. venv/bin/python -m pytest tests/integration/test_p4_evolution_strategy.py -q --tb=no"

echo ""
echo "=== 4. Cron Scripts Verification ==="
echo ""

check_item "Memory consolidation script exists" \
    "test -x tools/cron/memory_consolidation.sh"

check_item "Experiment cleanup script exists" \
    "test -x tools/cron/experiment_cleanup.sh"

check_item "Health check script exists" \
    "test -x tools/cron/health_check.sh"

check_item "Cron install script exists" \
    "test -x tools/cron/install_crontab.sh"

check_item "Cron uninstall script exists" \
    "test -x tools/cron/uninstall_crontab.sh"

echo ""
echo "=== 5. Functional Verification ==="
echo ""

# Test Memory Consolidator initialization
check_item "MemoryConsolidator initializes correctly" \
    "PYTHONPATH=. venv/bin/python -c 'from scripts.ai_trader.memory.memory_consolidator import MemoryConsolidator; m = MemoryConsolidator(); assert m is not None'"

# Test VariantComparator initialization
check_item "VariantComparator initializes correctly" \
    "PYTHONPATH=. venv/bin/python -c 'from libs.experiment.variant_comparator import VariantComparator; v = VariantComparator(); assert v is not None'"

# Test EvolutionStrategyManager initialization
check_item "EvolutionStrategyManager initializes correctly" \
    "PYTHONPATH=. venv/bin/python -c 'from libs.evolution.evolution_strategy_manager import EvolutionStrategyManager; e = EvolutionStrategyManager(); assert e is not None'"

echo ""
echo "========================================"
echo "Verification Summary"
echo "========================================"
echo -e "${GREEN}Passed: $pass_count${NC}"
echo -e "${RED}Failed: $fail_count${NC}"
echo ""

if [ $fail_count -eq 0 ]; then
    echo -e "${GREEN}✅ All checks passed! P3-P4 implementation is complete and verified.${NC}"
    exit 0
else
    echo -e "${YELLOW}⚠️  Some checks failed. Please review the failures above.${NC}"
    exit 1
fi
