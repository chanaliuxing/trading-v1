#!/bin/bash
#
# 盘前晨检脚本 - 每日 07:00-09:00 运行
# 检查系统健康、数据完整性、IBKR 连接
#

set -e

TRADING_ROOT="."
cd "$TRADING_ROOT"

echo "=============================================="
echo "🌅 盘前系统检查 - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=============================================="
echo ""

# 1. 基础设施健康检查
echo "1️⃣ 检查基础设施容器..."
echo "---"
cd "$TRADING_ROOT/infra"
docker-compose ps | grep -E "trading_(kafka|clickhouse|redis|schema)" | while read line; do
    container=$(echo $line | awk '{print $1}')
    status=$(echo $line | awk '{print $NF}')
    if [[ "$status" == *"healthy"* ]] || [[ "$status" == "Up" ]]; then
        echo "  ✅ $container: $status"
    else
        echo "  ❌ $container: $status"
    fi
done
echo ""

# 2. run_system.py 运行状态
echo "2️⃣ 检查 run_system.py..."
echo "---"
if pgrep -f "run_system.py" > /dev/null; then
    echo "  ✅ run_system.py 正在运行 (PID: $(pgrep -f run_system.py))"

    # 检查最近错误
    error_count=$(grep -c -E "(ERROR|CRITICAL)" "$TRADING_ROOT/logs/trading_system.log" 2>/dev/null | tail -100 || echo "0")
    if [ "$error_count" -gt 5 ]; then
        echo "  ⚠️ 发现 $error_count 个错误，请检查日志"
        tail -10 "$TRADING_ROOT/logs/trading_system.log" | grep -E "(ERROR|CRITICAL)"
    else
        echo "  ✅ 最近无严重错误"
    fi
else
    echo "  ❌ run_system.py 未运行！"
    echo "  💡 启动命令: cd $TRADING_ROOT && nohup python run_system.py > logs/run_system.log 2>&1 &"
fi
echo ""

# 3. IBKR 连接检查
echo "3️⃣ 检查 IBKR 连接..."
echo "---"
if [ -f "$TRADING_ROOT/temp/check_ibkr_status.py" ]; then
    python "$TRADING_ROOT/temp/check_ibkr_status.py" 2>&1 | head -10
else
    echo "  ⚠️ IBKR 检查脚本不存在，跳过"
fi
echo ""

# 4. 数据质量检查
echo "4️⃣ 检查数据质量..."
echo "---"

# 4.1 实时 Tick 数据
echo "  📊 最近 10 分钟 Tick 数据:"
clickhouse-client --query "
SELECT
    symbol,
    count() as tick_count,
    formatDateTime(max(ts_recv), '%H:%M:%S', 'America/New_York') as last_tick_et
FROM lake.ticks
WHERE ts_recv >= now() - INTERVAL 10 MINUTE
GROUP BY symbol
ORDER BY symbol
FORMAT Pretty
" 2>/dev/null || echo "    ⚠️ 无法查询 Tick 数据"

echo ""

# 4.2 昨日 Bar 数据完整性
echo "  📈 昨日 Bar 数据完整性:"
clickhouse-client --query "
SELECT
    symbol,
    count() as bars,
    CASE
        WHEN count() >= 390 THEN '✅'
        WHEN count() >= 350 THEN '⚠️'
        ELSE '❌'
    END as status
FROM trading.bars_1m
WHERE toDate(ts_end) = today() - 1
GROUP BY symbol
ORDER BY symbol
FORMAT Pretty
" 2>/dev/null || echo "    ⚠️ 无法查询 Bar 数据"

echo ""

# 4.3 检查需要回补的数据
echo "  🔄 检查缺失数据:"
missing=$(clickhouse-client --query "
SELECT count() as missing_count
FROM (
    SELECT symbol, count() as bars
    FROM trading.bars_1m
    WHERE toDate(ts_end) = today() - 1
    GROUP BY symbol
    HAVING bars < 390
)
FORMAT TabSeparated
" 2>/dev/null)

if [ -z "$missing" ] || [ "$missing" == "0" ]; then
    echo "    ✅ 昨日数据完整，无需回补"
else
    echo "    ⚠️ 发现 $missing 个股票数据缺失"
    clickhouse-client --query "
    SELECT
        symbol,
        count() as bars,
        390 - count() as missing
    FROM trading.bars_1m
    WHERE toDate(ts_end) = today() - 1
    GROUP BY symbol
    HAVING bars < 390
    ORDER BY missing DESC
    FORMAT Pretty
    " 2>/dev/null
    echo "    💡 运行 backfill 脚本: python $TRADING_ROOT/temp/trigger_backfill.py"
fi

echo ""

# 5. Kafka Consumer 状态
echo "5️⃣ 检查 Kafka Consumer Groups..."
echo "---"
docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 \
    --list 2>/dev/null | while read group; do
    lag=$(docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
        --bootstrap-server localhost:9092 \
        --describe --group "$group" 2>/dev/null | \
        awk 'NR>1 {sum+=$5} END {print sum}')

    if [ -z "$lag" ] || [ "$lag" == "0" ]; then
        echo "  ✅ $group: LAG=0"
    elif [ "$lag" -lt 1000 ]; then
        echo "  ⚠️ $group: LAG=$lag"
    else
        echo "  ❌ $group: LAG=$lag (高延迟!)"
    fi
done 2>/dev/null || echo "  ⚠️ 无法查询 Kafka Consumer Groups"

echo ""

# 6. 总结
echo "=============================================="
echo "✅ 盘前检查完成"
echo "=============================================="
echo ""
echo "📋 下一步操作:"
echo "  1. 如有异常，请手动修复"
echo "  2. 启动盘前 LLM Trader: cd $TRADING_ROOT/llm_trader && ./run_premarket.sh"
echo "  3. 监控系统日志: tail -f $TRADING_ROOT/logs/trading_system.log"
echo ""
