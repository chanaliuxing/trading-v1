#!/bin/bash
# AI Trader Test Runner Script

set -e

PROJECT_ROOT="."
cd "$PROJECT_ROOT"

echo "===================================="
echo "AI Trader Test Suite"
echo "===================================="
echo

# Check if pytest is installed
if ! python -m pytest --version &> /dev/null; then
    echo "❌ pytest not found. Installing test dependencies..."
    pip install pytest pytest-mock pytest-asyncio
fi

# Function to run tests
run_tests() {
    local test_path=$1
    local description=$2

    echo "Running: $description"
    echo "------------------------------------"

    if python -m pytest "$test_path" -v --tb=short; then
        echo "✅ $description: PASSED"
    else
        echo "⚠️  $description: Some tests failed (see above)"
    fi
    echo
}

# Run test suites
echo "1. Unit Tests - Gates"
run_tests "scripts/ai_trader/tests/unit/test_gates.py" "Gate Modules"

echo "2. Unit Tests - Orchestrator"
run_tests "scripts/ai_trader/tests/unit/test_orchestrator.py" "Orchestrator Logic"

echo "3. Unit Tests - Controllers"
run_tests "scripts/ai_trader/tests/unit/test_controllers.py" "Controller Modules"

echo "4. Unit Tests - AB Deployer"
run_tests "scripts/ai_trader/tests/unit/test_ab_deployer.py" "AB Testing"

echo "5. Integration Tests"
run_tests "scripts/ai_trader/tests/integration/" "Integration Workflows"

echo "===================================="
echo "Test Suite Complete"
echo "===================================="
echo

# Optional: Run with coverage if pytest-cov is installed
if python -c "import pytest_cov" &> /dev/null; then
    echo "Generating coverage report..."
    python -m pytest scripts/ai_trader/tests/unit/ \
        --cov=scripts.ai_trader \
        --cov-report=term-missing \
        --cov-report=html:scripts/ai_trader/htmlcov \
        -q

    echo
    echo "✅ Coverage report generated: scripts/ai_trader/htmlcov/index.html"
else
    echo "ℹ️  Install pytest-cov for coverage reports: pip install pytest-cov"
fi
