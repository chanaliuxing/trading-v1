#!/bin/bash
#
# 盘后复盘脚本 - 每日 16:30-20:00 运行
# LLM 决策复盘、参数优化、策略回测
#

set -e

TRADING_ROOT="."
cd "$TRADING_ROOT"

TODAY=$(date +%Y%m%d)
YESTERDAY=$(date -v-1d +%Y%m%d)

echo "=============================================="
echo "🌆 盘后复盘分析 - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=============================================="
echo ""

# 1. 导出今日 LLM 决策数据
echo "1️⃣ 导出今日 LLM 决策数据..."
echo "---"
mkdir -p "$TRADING_ROOT/llm_trader/results"

clickhouse-client --query "
SELECT
    formatDateTime(ts, '%Y-%m-%d %H:%M:%S', 'America/New_York') as time_et,
    symbol,
    provider,
    decision,
    confidence,
    reasoning,
    executed,
    entry_price,
    exit_price,
    pnl
FROM llm_trader.decisions
WHERE toDate(ts) = today()
ORDER BY ts
FORMAT CSVWithNames
" > "$TRADING_ROOT/llm_trader/results/decisions_$TODAY.csv" 2>/dev/null

if [ -f "$TRADING_ROOT/llm_trader/results/decisions_$TODAY.csv" ]; then
    decision_count=$(wc -l < "$TRADING_ROOT/llm_trader/results/decisions_$TODAY.csv")
    decision_count=$((decision_count - 1))  # 减去表头
    echo "  ✅ 导出 $decision_count 条决策记录"
    echo "  📁 文件: llm_trader/results/decisions_$TODAY.csv"
else
    echo "  ⚠️ 今日无决策记录或查询失败"
fi
echo ""

# 2. 今日交易统计
echo "2️⃣ 今日交易统计..."
echo "---"
clickhouse-client --query "
WITH stats AS (
    SELECT
        count() as total_decisions,
        sum(CASE WHEN executed = 1 THEN 1 ELSE 0 END) as executed_trades,
        sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        sum(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
        sum(pnl) as total_pnl,
        avg(pnl) as avg_pnl,
        max(pnl) as best_trade,
        min(pnl) as worst_trade
    FROM llm_trader.decisions
    WHERE toDate(ts) = today() AND executed = 1
)
SELECT
    '总决策数' as metric, toString(total_decisions) as value FROM stats
UNION ALL
SELECT '执行交易数', toString(executed_trades) FROM stats
UNION ALL
SELECT '胜率', concat(toString(round(winning_trades * 100.0 / nullIf(executed_trades, 0), 2)), '%') FROM stats
UNION ALL
SELECT '总盈亏', concat('$', toString(round(total_pnl, 2))) FROM stats
UNION ALL
SELECT '平均盈亏', concat('$', toString(round(avg_pnl, 2))) FROM stats
UNION ALL
SELECT '最佳交易', concat('$', toString(round(best_trade, 2))) FROM stats
UNION ALL
SELECT '最差交易', concat('$', toString(round(worst_trade, 2))) FROM stats
FORMAT Pretty
" 2>/dev/null || echo "  ⚠️ 无法查询交易统计"

echo ""

# 3. 各模型表现对比
echo "3️⃣ 各模型表现对比..."
echo "---"
clickhouse-client --query "
SELECT
    provider,
    count() as decisions,
    sum(CASE WHEN executed = 1 THEN 1 ELSE 0 END) as executed,
    sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    sum(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
    round(sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / nullIf(count(), 0), 2) as win_rate_pct,
    round(sum(pnl), 2) as total_pnl,
    round(avg(pnl), 2) as avg_pnl
FROM llm_trader.decisions
WHERE toDate(ts) = today()
GROUP BY provider
ORDER BY total_pnl DESC
FORMAT Pretty
" 2>/dev/null || echo "  ⚠️ 无法查询模型表现"

echo ""

# 4. 各股票表现
echo "4️⃣ 各股票表现..."
echo "---"
clickhouse-client --query "
SELECT
    symbol,
    count() as decisions,
    sum(CASE WHEN executed = 1 THEN 1 ELSE 0 END) as executed,
    round(sum(pnl), 2) as total_pnl,
    round(avg(pnl), 2) as avg_pnl
FROM llm_trader.decisions
WHERE toDate(ts) = today()
GROUP BY symbol
ORDER BY total_pnl DESC
FORMAT Pretty
" 2>/dev/null || echo "  ⚠️ 无法查询股票表现"

echo ""

# 5. 最佳和最差决策
echo "5️⃣ Top 5 最佳决策..."
echo "---"
clickhouse-client --query "
SELECT
    formatDateTime(ts, '%H:%M:%S', 'America/New_York') as time,
    symbol,
    provider,
    decision,
    round(pnl, 2) as pnl,
    substring(reasoning, 1, 60) as reasoning_preview
FROM llm_trader.decisions
WHERE toDate(ts) = today() AND executed = 1
ORDER BY pnl DESC
LIMIT 5
FORMAT Pretty
" 2>/dev/null || echo "  ⚠️ 无数据"

echo ""
echo "6️⃣ Top 5 最差决策..."
echo "---"
clickhouse-client --query "
SELECT
    formatDateTime(ts, '%H:%M:%S', 'America/New_York') as time,
    symbol,
    provider,
    decision,
    round(pnl, 2) as pnl,
    substring(reasoning, 1, 60) as reasoning_preview
FROM llm_trader.decisions
WHERE toDate(ts) = today() AND executed = 1
ORDER BY pnl ASC
LIMIT 5
FORMAT Pretty
" 2>/dev/null || echo "  ⚠️ 无数据"

echo ""

# 7. 运行 Alpha Loop 优化（可选）
echo "7️⃣ Alpha Loop 参数优化..."
echo "---"
read -p "是否运行 Alpha Loop 优化？(需要 10-30 分钟) [y/N]: " run_alpha

if [[ "$run_alpha" =~ ^[Yy]$ ]]; then
    echo "  🔄 启动 Alpha Loop (快速模式)..."

    python "$TRADING_ROOT/alpha_loop.py" \
        --model xgboost \
        --symbols AAPL TSLA NVDA \
        --lookback-days 7 \
        --n-trials 20 \
        --output "results/alpha_loop_quick_$TODAY.json" \
        2>&1 | tee "logs/alpha_loop_$TODAY.log"

    if [ -f "results/alpha_loop_quick_$TODAY.json" ]; then
        echo "  ✅ Alpha Loop 完成"
        echo "  📁 结果: results/alpha_loop_quick_$TODAY.json"

        # 应用新参数
        read -p "是否应用新参数到配置？[y/N]: " apply_params
        if [[ "$apply_params" =~ ^[Yy]$ ]]; then
            python "$TRADING_ROOT/apply_hpo_params.py" \
                --results "results/alpha_loop_quick_$TODAY.json" \
                --config config.yml \
                --backup
            echo "  ✅ 参数已更新，配置已备份"
            echo "  💡 热重载参数: python hot_reload_test_params.py"
        fi
    else
        echo "  ❌ Alpha Loop 失败，请查看日志: logs/alpha_loop_$TODAY.log"
    fi
else
    echo "  ⏭️ 跳过 Alpha Loop 优化"
fi

echo ""

# 8. 总结和建议
echo "=============================================="
echo "✅ 盘后复盘完成"
echo "=============================================="
echo ""
echo "📋 复盘要点:"

# 计算胜率和盈亏
win_rate=$(clickhouse-client --query "
SELECT round(sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / nullIf(count(), 0), 1)
FROM llm_trader.decisions
WHERE toDate(ts) = today() AND executed = 1
FORMAT TabSeparated
" 2>/dev/null)

total_pnl=$(clickhouse-client --query "
SELECT round(sum(pnl), 2)
FROM llm_trader.decisions
WHERE toDate(ts) = today() AND executed = 1
FORMAT TabSeparated
" 2>/dev/null)

if [ ! -z "$win_rate" ]; then
    echo "  📊 今日胜率: $win_rate%"

    if (( $(echo "$win_rate < 50" | bc -l) )); then
        echo "  ⚠️ 胜率低于 50%，建议："
        echo "     - 检查决策逻辑是否合理"
        echo "     - 优化 Prompt 增加风险评估"
        echo "     - 调整置信度阈值"
    else
        echo "  ✅ 胜率良好"
    fi
fi

if [ ! -z "$total_pnl" ]; then
    echo "  💰 今日盈亏: \$$total_pnl"

    if (( $(echo "$total_pnl < 0" | bc -l) )); then
        echo "  ⚠️ 今日亏损，需要分析原因："
        echo "     - 查看最差决策的共同特征"
        echo "     - 检查市场环境是否适合当前策略"
    else
        echo "  ✅ 今日盈利"
    fi
fi

echo ""
echo "🔗 相关文件:"
echo "  📊 决策数据: llm_trader/results/decisions_$TODAY.csv"
echo "  📈 优化结果: results/alpha_loop_quick_$TODAY.json (如已运行)"
echo "  📝 日志文件: logs/alpha_loop_$TODAY.log (如已运行)"
echo ""
echo "📌 下一步:"
echo "  1. 分析决策数据，找出成功和失败的模式"
echo "  2. 优化 Prompt: vim llm_trader/config/prompts.yaml"
echo "  3. 如已运行 Alpha Loop，考虑更新策略参数"
echo "  4. 准备明日交易：启动隔夜 LLM Trader"
echo ""
