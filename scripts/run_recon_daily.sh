#!/bin/bash
##############################################################################
# Wrapper script for daily recon-all with proper Python venv activation
##############################################################################

set -e

# Unbuffer output for immediate logging
export PYTHONUNBUFFERED=1

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Log start time
echo "=================================="
echo "Trading Recon Daily - Started at $(date)"
echo "=================================="
echo "Project root: $PROJECT_ROOT"
echo ""

# Activate virtual environment
echo "Activating Python virtual environment..."
source "$PROJECT_ROOT/venv/bin/activate"
echo "Python: $(which python3)"
echo "Python version: $(python3 --version)"
echo ""

# Change to project directory
cd "$PROJECT_ROOT"
echo "Changed to: $(pwd)"
echo ""

# Run make recon-all
echo "Running: make recon-all"
echo ""
exec make recon-all
