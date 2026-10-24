#!/bin/bash
# Memory Consolidation Cron Job
# 每日运行 STS → MTS → LTS 升级

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs/cron"
LOG_FILE="$LOG_DIR/memory_consolidation_$(date +%Y%m%d).log"

# Create log directory
mkdir -p "$LOG_DIR"

# Log start
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== Memory Consolidation Started =====" >> "$LOG_FILE"

# Activate virtual environment
source "$PROJECT_ROOT/venv/bin/activate"

# Change to project directory
cd "$PROJECT_ROOT"

# Run STS → MTS promotion
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running STS → MTS promotion..." >> "$LOG_FILE"
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/memory_consolidator.py \
    --mode promote_sts_to_mts \
    --min-access-count 5 \
    --min-importance 0.7 \
    >> "$LOG_FILE" 2>&1

# Run MTS → LTS promotion
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running MTS → LTS promotion..." >> "$LOG_FILE"
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/memory_consolidator.py \
    --mode promote_mts_to_lts \
    --min-verification 5 \
    --min-stability 0.8 \
    >> "$LOG_FILE" 2>&1

# Log completion
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== Memory Consolidation Completed =====" >> "$LOG_FILE"

# Rotate logs (keep last 30 days)
find "$LOG_DIR" -name "memory_consolidation_*.log" -mtime +30 -delete

exit 0
