#!/bin/bash
# Test runner script for CI/CD
# Usage:
#   ./tests/run_all_tests.sh              # Run fast tests only
#   ./tests/run_all_tests.sh --all        # Run all tests including slow ones
#   ./tests/run_all_tests.sh --unit       # Run unit tests only
#   ./tests/run_all_tests.sh --integration # Run integration tests only
#   ./tests/run_all_tests.sh --e2e        # Run E2E tests only

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Navigate to project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo "========================================"
echo "Trading System Test Suite"
echo "========================================"
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}ERROR: pytest not found. Install with: pip install pytest${NC}"
    exit 1
fi

# Check if virtual environment is activated
if [ -z "$VIRTUAL_ENV" ]; then
    echo -e "${YELLOW}WARNING: No virtual environment detected. Attempting to activate...${NC}"
    if [ -f "venv/bin/activate" ]; then
        source venv/bin/activate
        echo -e "${GREEN}Virtual environment activated${NC}"
    else
        echo -e "${RED}ERROR: Virtual environment not found. Run: python -m venv venv${NC}"
        exit 1
    fi
fi

# Parse arguments
RUN_MODE="fast"  # Default: run fast tests only

if [ "$1" == "--all" ]; then
    RUN_MODE="all"
elif [ "$1" == "--unit" ]; then
    RUN_MODE="unit"
elif [ "$1" == "--integration" ]; then
    RUN_MODE="integration"
elif [ "$1" == "--e2e" ]; then
    RUN_MODE="e2e"
elif [ "$1" == "--help" ] || [ "$1" == "-h" ]; then
    echo "Usage:"
    echo "  ./tests/run_all_tests.sh              # Run fast tests only (default)"
    echo "  ./tests/run_all_tests.sh --all        # Run all tests including slow ones"
    echo "  ./tests/run_all_tests.sh --unit       # Run unit tests only"
    echo "  ./tests/run_all_tests.sh --integration # Run integration tests only"
    echo "  ./tests/run_all_tests.sh --e2e        # Run E2E tests only"
    echo ""
    exit 0
fi

# Run tests based on mode
case $RUN_MODE in
    "fast")
        echo -e "${GREEN}Running fast tests (unit tests only, skipping slow tests)...${NC}"
        pytest tests/unit/ -v --tb=short -m "not slow" || {
            echo -e "${RED}Fast tests failed!${NC}"
            exit 1
        }
        ;;

    "all")
        echo -e "${GREEN}Running ALL tests (unit + integration + E2E, including slow tests)...${NC}"
        pytest tests/ -v --tb=short || {
            echo -e "${RED}Tests failed!${NC}"
            exit 1
        }
        ;;

    "unit")
        echo -e "${GREEN}Running unit tests...${NC}"
        pytest tests/unit/ -v --tb=short || {
            echo -e "${RED}Unit tests failed!${NC}"
            exit 1
        }
        ;;

    "integration")
        echo -e "${GREEN}Running integration tests...${NC}"
        pytest tests/integration/ -v --tb=short -m "integration" || {
            echo -e "${RED}Integration tests failed!${NC}"
            exit 1
        }
        ;;

    "e2e")
        echo -e "${GREEN}Running E2E tests...${NC}"
        pytest tests/e2e/ -v --tb=short -m "e2e" || {
            echo -e "${RED}E2E tests failed!${NC}"
            exit 1
        }
        ;;
esac

echo ""
echo -e "${GREEN}========================================"
echo "All tests passed! ✓"
echo "========================================${NC}"

# Generate test summary
echo ""
echo "Test Summary:"
echo "-------------"
echo "Test mode: $RUN_MODE"
echo "Date: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Optional: Generate coverage report
# Uncomment if pytest-cov is installed
# echo "Generating coverage report..."
# pytest tests/ --cov=. --cov-report=html --cov-report=term

exit 0
