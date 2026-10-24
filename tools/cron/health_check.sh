#!/bin/bash
# Health Check Cron Job
# 每小时检查 P3-P4 组件健康状态

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs/cron"
LOG_FILE="$LOG_DIR/health_check_$(date +%Y%m%d).log"

mkdir -p "$LOG_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== Health Check Started =====" >> "$LOG_FILE"

# Check ClickHouse connectivity
echo -n "[$(date '+%Y-%m-%d %H:%M:%S')] Checking ClickHouse... " >> "$LOG_FILE"
if docker exec trading_clickhouse clickhouse-client --query "SELECT 1" > /dev/null 2>&1; then
    echo "OK" >> "$LOG_FILE"
else
    echo "FAILED" >> "$LOG_FILE"
    # Send alert (TODO: integrate with alerting system)
fi

# Check Memory Fabric tables
echo -n "[$(date '+%Y-%m-%d %H:%M:%S')] Checking Memory Fabric tables... " >> "$LOG_FILE"
TABLE_COUNT=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM system.tables WHERE database = 'memory_fabric'" 2>/dev/null || echo "0")
if [ "$TABLE_COUNT" -ge "5" ]; then
    echo "OK ($TABLE_COUNT tables)" >> "$LOG_FILE"
else
    echo "WARNING (only $TABLE_COUNT tables)" >> "$LOG_FILE"
fi

# Check Evolution tables
echo -n "[$(date '+%Y-%m-%d %H:%M:%S')] Checking Evolution tables... " >> "$LOG_FILE"
EVOLUTION_TABLE_COUNT=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM system.tables WHERE database = 'evolution'" 2>/dev/null || echo "0")
if [ "$EVOLUTION_TABLE_COUNT" -ge "4" ]; then
    echo "OK ($EVOLUTION_TABLE_COUNT tables)" >> "$LOG_FILE"
else
    echo "WARNING (only $EVOLUTION_TABLE_COUNT tables)" >> "$LOG_FILE"
fi

# Check STS record count
STS_COUNT=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM memory_fabric.short_term" 2>/dev/null || echo "ERROR")
echo "[$(date '+%Y-%m-%d %H:%M:%S')] STS records: $STS_COUNT" >> "$LOG_FILE"

# Check MTS record count
MTS_COUNT=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM memory_fabric.mid_term" 2>/dev/null || echo "ERROR")
echo "[$(date '+%Y-%m-%d %H:%M:%S')] MTS records: $MTS_COUNT" >> "$LOG_FILE"

# Check LTS record count
LTS_COUNT=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM memory_fabric.long_term" 2>/dev/null || echo "ERROR")
echo "[$(date '+%Y-%m-%d %H:%M:%S')] LTS records: $LTS_COUNT" >> "$LOG_FILE"

# Check patch queue
PATCH_PENDING=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM evolution.patch_approval_queue WHERE status = 'pending'" 2>/dev/null || echo "ERROR")
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pending patches: $PATCH_PENDING" >> "$LOG_FILE"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== Health Check Completed =====" >> "$LOG_FILE"

# Only keep today's health check log
find "$LOG_DIR" -name "health_check_*.log" -mtime +1 -delete

exit 0
