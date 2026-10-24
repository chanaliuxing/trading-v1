#!/bin/bash
# Experiment Cleanup Cron Job
# 每周清理过期的实验记录和 patch 队列

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs/cron"
LOG_FILE="$LOG_DIR/experiment_cleanup_$(date +%Y%m%d).log"

mkdir -p "$LOG_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== Experiment Cleanup Started =====" >> "$LOG_FILE"

# Activate virtual environment
source "$PROJECT_ROOT/venv/bin/activate"

# Change to project directory
cd "$PROJECT_ROOT"

# Clean up old experiments (older than 90 days)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleaning up old experiments..." >> "$LOG_FILE"

# SQL cleanup script
docker exec trading_clickhouse clickhouse-client --query "
    -- Delete old pending patches (older than 30 days)
    DELETE FROM evolution.patch_approval_queue
    WHERE status = 'pending'
      AND created_at < now() - INTERVAL 30 DAY;

    -- Delete old rejected patches (older than 90 days)
    DELETE FROM evolution.patch_approval_queue
    WHERE status = 'rejected'
      AND created_at < now() - INTERVAL 90 DAY;

    -- Optimize tables
    OPTIMIZE TABLE evolution.patch_approval_queue FINAL;
    OPTIMIZE TABLE evolution.patch_evaluation_details FINAL;
    OPTIMIZE TABLE evolution.patch_comparison FINAL;
" >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleanup completed" >> "$LOG_FILE"

# Log completion
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== Experiment Cleanup Completed =====" >> "$LOG_FILE"

# Rotate logs (keep last 30 days)
find "$LOG_DIR" -name "experiment_cleanup_*.log" -mtime +30 -delete

exit 0
