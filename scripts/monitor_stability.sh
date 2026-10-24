#!/bin/bash
# Continuous stability monitor - logs health metrics every 30 minutes
# Usage: ./scripts/monitor_stability.sh &

LOG_FILE="/workspace/grafana-stack/logs/trader.log"
MONITOR_LOG="logs/stability_monitor.log"
CHECK_INTERVAL=1800  # 30 minutes

echo "========================================" | tee -a "$MONITOR_LOG"
echo "Stability Monitor Started: $(date)" | tee -a "$MONITOR_LOG"
echo "========================================" | tee -a "$MONITOR_LOG"

while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "" | tee -a "$MONITOR_LOG"
    echo "[$TIMESTAMP] Health Check" | tee -a "$MONITOR_LOG"
    echo "----------------------------------------" | tee -a "$MONITOR_LOG"

    # Check run_system.py
    if PID=$(pgrep -f "python.*run_system.py"); then
        UPTIME=$(ps -o etime= -p "$PID" | tr -d ' ')
        echo "✓ run_system.py: UP (PID: $PID, Uptime: $UPTIME)" | tee -a "$MONITOR_LOG"
    else
        echo "✗ run_system.py: DOWN" | tee -a "$MONITOR_LOG"
    fi

    # Check Kafka brokers
    KAFKA_COUNT=$(docker ps --filter "name=trading_kafka" --filter "health=healthy" --format "{{.Names}}" | wc -l | tr -d ' ')
    echo "✓ Kafka brokers healthy: $KAFKA_COUNT/2" | tee -a "$MONITOR_LOG"

    # Check for recent Kafka errors (last 30 min)
    ERROR_COUNT=$(grep "$(date -v-30M '+%Y-%m-%d').*UnknownError\|coordinator.*fail" "$LOG_FILE" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$ERROR_COUNT" -gt 0 ]; then
        echo "⚠ Kafka errors in last 30min: $ERROR_COUNT" | tee -a "$MONITOR_LOG"
        echo "Last 3 errors:" | tee -a "$MONITOR_LOG"
        grep "UnknownError\|coordinator.*fail" "$LOG_FILE" | tail -3 | tee -a "$MONITOR_LOG"
    else
        echo "✓ No Kafka errors in last 30min" | tee -a "$MONITOR_LOG"
    fi

    # Check consumer groups
    ACTIVE_CONSUMERS=$(docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
        --bootstrap-server localhost:19092 --list 2>/dev/null | \
        while read group; do
            docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
                --bootstrap-server localhost:19092 --describe --group "$group" 2>/dev/null | \
                grep -c "aiokafka" || echo "0"
        done | grep -v "^0$" | wc -l | tr -d ' ')
    echo "✓ Active consumer groups: $ACTIVE_CONSUMERS" | tee -a "$MONITOR_LOG"

    # Check ClickHouse
    if curl -s "http://localhost:8123/?query=SELECT%201" >/dev/null 2>&1; then
        echo "✓ ClickHouse: UP" | tee -a "$MONITOR_LOG"
    else
        echo "✗ ClickHouse: DOWN" | tee -a "$MONITOR_LOG"
    fi

    echo "----------------------------------------" | tee -a "$MONITOR_LOG"

    sleep "$CHECK_INTERVAL"
done
