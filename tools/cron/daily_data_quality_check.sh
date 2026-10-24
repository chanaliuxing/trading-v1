#!/bin/bash
# Daily Data Quality Check and Integration
# Runs data quality monitoring, merges results into mutable_targets, and emits events

set -e

TRADING_ROOT="."
cd "$TRADING_ROOT"

echo "[$(date)] Starting daily data quality check..."

# 1. Run data quality monitor
echo "[$(date)] Running data quality monitor..."
PYTHONPATH=. venv/bin/python tools/data_quality_monitor.py \
    --symbols TSLA NVDA AAPL MSFT \
    --days 7 \
    --output-dir runs/data_quality

# 2. Merge results into mutable_targets
echo "[$(date)] Merging data quality results into mutable_targets..."
PYTHONPATH=. venv/bin/python tools/merge_data_quality_to_mutable_targets.py

# 3. Emit events for unhealthy targets
echo "[$(date)] Scanning and emitting events for unhealthy targets..."
PYTHONPATH=. venv/bin/python tools/emit_unhealthy_target_events.py --threshold 0.5

echo "[$(date)] ✅ Daily data quality check completed"
