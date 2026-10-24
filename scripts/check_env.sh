#!/bin/bash

set -e

echo "=================================="
echo "Environment Health Check"
echo "=================================="

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

check_service() {
    local service=$1
    local host=$2
    local port=$3
    local protocol=${4:-tcp}

    echo -n "Checking $service ($host:$port)... "

    if [ "$protocol" = "http" ]; then
        if curl -s -f -o /dev/null "$host:$port" 2>/dev/null; then
            echo -e "${GREEN}OK${NC}"
            return 0
        else
            echo -e "${RED}FAIL${NC}"
            return 1
        fi
    else
        if nc -z -w 2 "$host" "$port" 2>/dev/null; then
            echo -e "${GREEN}OK${NC}"
            return 0
        else
            echo -e "${RED}FAIL${NC}"
            return 1
        fi
    fi
}

# Check .env file
if [ -f .env ]; then
    echo -e "${GREEN}.env file found${NC}"
    source .env
else
    echo -e "${RED}.env file not found${NC}"
    exit 1
fi

# Check PostgreSQL
check_service "PostgreSQL" "${PG_HOST:-localhost}" "${PG_PORT:-5432}"

# Check Redis
check_service "Redis" "localhost" "6379"

# Check Kafka
check_service "Kafka" "localhost" "9092"

# Check Zookeeper (skip when running Kafka KRaft mode)
if [ "${KAFKA_MODE:-}" = "kraft" ] || [ "${USE_ZOOKEEPER:-}" = "false" ] || [ -n "${KAFKA_PROCESS_ROLES:-}" ]; then
    echo -e "Checking Zookeeper (localhost:2181)... ${YELLOW}SKIP (KRaft mode)${NC}"
else
    check_service "Zookeeper" "localhost" "2181"
fi

# Check Schema Registry
check_service "Schema Registry" "http://localhost" "8081" "http"

# Check ClickHouse HTTP
check_service "ClickHouse HTTP" "http://localhost" "8123" "http"

# Check ClickHouse Native
check_service "ClickHouse Native" "localhost" "9000"

# Check Prometheus
check_service "Prometheus" "http://localhost" "9090" "http"

# Check Grafana
check_service "Grafana" "http://localhost" "${GRAFANA_PORT:-3000}" "http"

echo ""
echo "=================================="
echo "Database Connection Tests"
echo "=================================="

# Test PostgreSQL connection
echo -n "Testing PostgreSQL connection... "
if PGPASSWORD=$PG_PASSWORD psql -h $PG_HOST -U $PG_USER -d $PG_DB -c "SELECT 1;" > /dev/null 2>&1; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAIL${NC}"
fi

# Test ClickHouse connection
echo -n "Testing ClickHouse connection... "
if curl -s "http://localhost:8123/?query=SELECT%201" | grep -q "1"; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAIL${NC}"
fi

# Test Redis connection
echo -n "Testing Redis connection... "
if redis-cli -h 127.0.0.1 ping > /dev/null 2>&1; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAIL${NC}"
fi

echo ""
echo "=================================="
echo "Health Check Complete"
echo "=================================="

echo ""
echo "=================================="
echo "LLM Trader Realtime Readiness (Redis 1s)"
echo "=================================="

if command -v python >/dev/null 2>&1; then
  if [ -f tools/check_llm_trader_realtime.py ]; then
    RT_JSON=$(python tools/check_llm_trader_realtime.py --json 2>/dev/null || echo '{"ok": false, "error": "exec_failed"}')
    echo "$RT_JSON" | python - << 'PY'
import sys, json
data = json.load(sys.stdin)
ok = data.get('ok')
print('OK' if ok else 'FAIL')
PY
    echo "$RT_JSON" | sed -n 's/^[ ]*//;p'
  else
    echo -e "${YELLOW}tools/check_llm_trader_realtime.py not found, skipping${NC}"
  fi
else
  echo -e "${YELLOW}python not found, skipping LLM Trader readiness${NC}"
fi

echo ""
echo "=================================="
echo "ClickHouse Strategy Freshness (per enabled strategy/symbol)"
echo "=================================="

if command -v python >/dev/null 2>&1; then
  if [ -f tools/check_clickhouse_strategy_freshness.py ]; then
    CH_JSON=$(python tools/check_clickhouse_strategy_freshness.py --stale-sec 180 --json 2>/dev/null || echo '{"ok": false, "error": "exec_failed"}')
    echo "$CH_JSON" | python - << 'PY'
import sys, json
data = json.load(sys.stdin)
ok = data.get('ok')
print('OK' if ok else 'FAIL')
PY
    echo "$CH_JSON" | sed -n 's/^[ ]*//;p'
  else
    echo -e "${YELLOW}tools/check_clickhouse_strategy_freshness.py not found, skipping${NC}"
  fi
else
  echo -e "${YELLOW}python not found, skipping ClickHouse strategy freshness${NC}"
fi
