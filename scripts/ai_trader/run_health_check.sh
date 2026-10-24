#!/bin/bash
# Run AI Trader tools health check with proper PYTHONPATH

cd "$(dirname "$0")/../.."
export PYTHONPATH="$PWD:$PYTHONPATH"

echo "Running AI Trader Tools Health Check..."
echo "Working directory: $PWD"
echo ""

# Run pytest on health tests
python -m pytest scripts/ai_trader/tests/health/test_critical_tools.py -v --tb=short "$@"
