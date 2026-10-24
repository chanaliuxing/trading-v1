#!/bin/bash
# System Health Verification Script
# Checks Kafka, ClickHouse, Redis, PostgreSQL, and consumer group health

set -e

echo "=========================================="
echo "Trading System Health Check"
echo "=========================================="
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print status
print_status() {
    if [ $1 -eq 0 ]; then
        echo -e "${GREEN}✓${NC} $2"
    else
        echo -e "${RED}✗${NC} $2"
    fi
}

# 1. Check Docker Containers
echo "1. Checking Docker Containers..."
echo "--------------------------------"
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "kafka|clickhouse|redis|NAMES" || true
echo ""

# 2. Check Kafka Brokers
echo "2. Checking Kafka Cluster Health..."
echo "------------------------------------"
KAFKA_HEALTHY=$(docker ps --filter "name=trading_kafka" --filter "health=healthy" --format "{{.Names}}" | wc -l)
if [ "$KAFKA_HEALTHY" -eq 2 ]; then
    print_status 0 "Both Kafka brokers are healthy"
else
    print_status 1 "Kafka brokers not all healthy (found $KAFKA_HEALTHY/2)"
fi
echo ""

# 3. Check Consumer Offsets Topic
echo "3. Checking __consumer_offsets Topic..."
echo "----------------------------------------"
OFFSETS_RF=$(docker exec trading_kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:19092 \
    --describe --topic __consumer_offsets 2>/dev/null | \
    grep "ReplicationFactor: 2" | wc -l)

if [ "$OFFSETS_RF" -gt 0 ]; then
    print_status 0 "__consumer_offsets has RF=2 (both brokers)"
else
    print_status 1 "__consumer_offsets replication issue"
fi
echo ""

# 4. Check Consumer Groups
echo "4. Checking Kafka Consumer Groups..."
echo "--------------------------------------"
echo "Active Consumer Groups:"
docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:19092 \
    --list 2>/dev/null | while read group; do

    STATUS=$(docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
        --bootstrap-server localhost:19092 \
        --describe --group "$group" 2>/dev/null | grep -c "aiokafka" || echo "0")

    if [ "$STATUS" -gt 0 ]; then
        echo -e "  ${GREEN}✓${NC} $group (active)"
    else
        echo -e "  ${YELLOW}○${NC} $group (idle)"
    fi
done
echo ""

# 5. Check ClickHouse
echo "5. Checking ClickHouse..."
echo "-------------------------"
CH_RESPONSE=$(curl -s "http://localhost:8123/?query=SELECT%201" 2>/dev/null)
if [ "$CH_RESPONSE" = "1" ]; then
    print_status 0 "ClickHouse HTTP interface responding"
else
    print_status 1 "ClickHouse not responding"
fi
echo ""

# 6. Check Redis
echo "6. Checking Redis..."
echo "--------------------"
if nc -z localhost 6379 2>/dev/null; then
    print_status 0 "Redis accessible on localhost:6379"
else
    print_status 1 "Redis not accessible"
fi
echo ""

# 7. Check PostgreSQL
echo "7. Checking PostgreSQL..."
echo "-------------------------"
if nc -z localhost 5432 2>/dev/null; then
    print_status 0 "PostgreSQL accessible on localhost:5432"
else
    print_status 1 "PostgreSQL not accessible"
fi
echo ""

# 8. Check run_system.py
echo "8. Checking run_system.py Process..."
echo "-------------------------------------"
if pgrep -f "python.*run_system.py" > /dev/null; then
    PID=$(pgrep -f "python.*run_system.py")
    RUNTIME=$(ps -o etime= -p "$PID" | tr -d ' ')
    print_status 0 "run_system.py is running (PID: $PID, Runtime: $RUNTIME)"
else
    print_status 1 "run_system.py is NOT running"
fi
echo ""

# 9. Check Bull Put Strategy
echo "9. Checking Bull Put Spread Strategy..."
echo "----------------------------------------"
if grep -q "bull_put_spread_qqq" logs/trader.log 2>/dev/null; then
    print_status 0 "Bull Put strategy found in logs"

    LAST_EVENT=$(grep "bull_put" logs/trader.log 2>/dev/null | tail -1 | jq -r '.event' 2>/dev/null || echo "N/A")
    echo "  Last event: $LAST_EVENT"
else
    print_status 1 "Bull Put strategy not found in logs"
fi
echo ""

# 10. Check Recent Kafka Errors
echo "10. Checking Recent Kafka Errors (last 5 min)..."
echo "-------------------------------------------------"
ERROR_COUNT=$(docker logs trading_kafka --since 5m 2>&1 | grep -ci "error" || echo "0")
if [ "$ERROR_COUNT" -eq 0 ]; then
    print_status 0 "No errors in Kafka logs (last 5 minutes)"
else
    print_status 1 "Found $ERROR_COUNT error(s) in Kafka logs"
    echo "Recent errors:"
    docker logs trading_kafka --since 5m 2>&1 | grep -i "error" | tail -3
fi
echo ""

echo "=========================================="
echo "Health Check Complete"
echo "=========================================="
