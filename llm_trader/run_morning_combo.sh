#!/bin/bash
# 早晨交易组合：盘前(DeepSeek) + 开盘第一小时(Gemini)
# 4:00-10:30 ET 连续交易

set -e

# 确保在正确的目录
cd "$(dirname "$0")"

echo "================================================================================"
echo "  早晨交易组合策略"
echo "================================================================================"
echo ""
echo "📅 交易时段:"
echo "  ├─ 盘前交易:     4:00-9:25 ET  (DeepSeek V3, 每2分钟)"
echo "  └─ 开盘第一小时: 9:30-10:25 ET (Gemini 2.0, 每1分钟)"
echo ""
echo "🤖 Provider配置:"
echo "  - DeepSeek V3:   盘前交易 (稳健)"
echo "  - Gemini 2.0:    开盘冲刺 (快速)"
echo ""
echo "💰 资金配置:"
echo "  - 每个策略: \$50,000 独立账户"
echo "  - 总资金: \$100,000"
echo ""
echo "⏱️  预计运行时长: 6.5小时"
echo ""
echo "================================================================================"
echo ""

# 创建日志目录
mkdir -p logs

# 1. 启动盘前交易 (DeepSeek)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🟢 启动盘前交易 (DeepSeek)..."

../venv/bin/python run_single_bot.py \
  --provider deepseek \
  --interval 120 \
  --cycles 165 \
  --cash 50000 \
  --prefill-5s-mins 10 \
  --close-at "09:25" \
  --forbid-new-positions-before 5 \
  > logs/morning_premarket_$(date '+%Y%m%d_%H%M%S').log 2>&1 &

PREMARKET_PID=$!
echo "  ✅ 盘前交易已启动 - PID: $PREMARKET_PID"

# 等待2秒避免同时启动冲突
sleep 2

# 2. 启动开盘第一小时 (Gemini)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🟢 启动开盘第一小时 (Gemini)..."

../venv/bin/python run_single_bot.py \
  --provider gemini \
  --interval 60 \
  --cycles 55 \
  --cash 50000 \
  --prefill-5s-mins 10 \
  --close-at "10:25" \
  --forbid-new-positions-before 5 \
  --wait-for-market \
  > logs/morning_first_hour_$(date '+%Y%m%d_%H%M%S').log 2>&1 &

FIRST_HOUR_PID=$!
echo "  ✅ 开盘第一小时已启动 - PID: $FIRST_HOUR_PID"

echo ""
echo "================================================================================"
echo "✅ 早晨交易组合已全部启动！"
echo "================================================================================"
echo ""
echo "📊 运行状态:"
echo "  ├─ 盘前交易 (DeepSeek):     PID $PREMARKET_PID"
echo "  │  └─ 日志: logs/morning_premarket_*.log"
echo "  │"
echo "  └─ 开盘第一小时 (Gemini):   PID $FIRST_HOUR_PID"
echo "     └─ 日志: logs/morning_first_hour_*.log"
echo ""
echo "📈 监控命令:"
echo "  - 查看所有进程:  ps aux | grep 'run_single_bot'"
echo "  - 盘前日志:      tail -f logs/morning_premarket_*.log"
echo "  - 开盘日志:      tail -f logs/morning_first_hour_*.log"
echo "  - 停止所有:      kill $PREMARKET_PID $FIRST_HOUR_PID"
echo ""
echo "⏰ 时间表:"
echo "  - 当前时间:     $(date '+%H:%M:%S')"
echo "  - 盘前开始:     4:00 ET"
echo "  - 盘前结束:     9:25 ET"
echo "  - 开盘:         9:30 ET (Gemini自动等待)"
echo "  - 第一小时结束: 10:25 ET"
echo ""
echo "💡 提示:"
echo "  - 盘前交易现在开始运行"
echo "  - 开盘第一小时会等到9:30自动开始"
echo "  - 所有持仓会在各自结束时间自动清仓"
echo ""
echo "================================================================================"
echo ""
echo "等待所有策略完成..."
echo ""

# 等待盘前交易完成
wait $PREMARKET_PID
PREMARKET_EXIT=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 📊 盘前交易完成 (exit code: $PREMARKET_EXIT)"

# 等待开盘第一小时完成
wait $FIRST_HOUR_PID
FIRST_HOUR_EXIT=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 📊 开盘第一小时完成 (exit code: $FIRST_HOUR_EXIT)"

echo ""
echo "================================================================================"
echo "🎉 早晨交易组合全部完成！"
echo "================================================================================"
echo ""
echo "📁 查看结果:"
echo "  - 盘前结果:   grep 'Final Portfolio' logs/morning_premarket_*.log"
echo "  - 开盘结果:   grep 'Final Portfolio' logs/morning_first_hour_*.log"
echo "  - JSON结果:   ls -lh results/*_final_*.json | tail -2"
echo ""
echo "📊 综合分析:"
echo "  - 总交易时长: 6.5小时"
echo "  - 两个独立账户，可单独查看盈亏"
echo ""
