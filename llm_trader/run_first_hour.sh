#!/bin/bash
# 开盘第一小时交易 (9:30-10:30 ET) - Gemini 2.0 Flash
# 运行时间: 1小时 (60分钟)

set -e

# 确保在正确的目录
cd "$(dirname "$0")"

echo "================================================================================"
echo "  开盘第一小时策略 - Gemini 2.0 Flash"
echo "================================================================================"
echo ""
echo "交易时段:"
echo "  - 开始: 9:30 AM ET (开盘)"
echo "  - 结束: 10:25 AM ET (清仓，留5分钟缓冲)"
echo "  - 总时长: 1 小时 (60分钟)"
echo ""
echo "策略配置:"
echo "  - Provider: Gemini 2.0 Flash (快速响应)"
echo "  - Symbols: Universe 15只股票"
echo "  - Interval: 300秒 (5分钟低频决策)"
echo "  - Cycles: 11 (55分钟)"
echo "  - 初始资金: \$50,000"
echo ""
echo "风控设置:"
echo "  - 10:25 强制清仓 (--close-at)"
echo "  - 10:20后禁止开新仓 (--forbid-new-positions-before 5)"
echo ""
echo "策略特点:"
echo "  - 开盘第一小时流动性最好"
echo "  - 波动最大，适合快速进出"
echo "  - 新闻驱动明显"
echo "  - Gemini快速响应能力强"
echo ""
echo "================================================================================"
echo ""

# 创建日志目录
mkdir -p logs

# 启动 Gemini 开盘第一小时交易
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting First Hour Trading..."

../venv/bin/python run_single_bot.py \
  --provider gemini \
  --interval 300 \
  --cycles 11 \
  --cash 50000 \
  --prefill-5s-mins 10 \
  --close-at "10:25" \
  --forbid-new-positions-before 5 \
  --wait-for-market \
  > logs/first_hour_gemini_$(date '+%Y%m%d_%H%M%S').log 2>&1 &

FIRST_HOUR_PID=$!

echo ""
echo "================================================================================"
echo "开盘第一小时交易已启动！"
echo "================================================================================"
echo ""
echo "进程信息:"
echo "  - PID: $FIRST_HOUR_PID"
echo "  - Provider: Gemini 2.0 Flash"
echo "  - 运行时长: 55分钟 (11个周期 × 5分钟)"
echo ""
echo "监控命令:"
echo "  - 查看进程: ps aux | grep $FIRST_HOUR_PID"
echo "  - 实时日志: tail -f logs/first_hour_gemini_*.log"
echo "  - 停止交易: kill $FIRST_HOUR_PID"
echo ""
echo "预计完成时间:"
echo "  - 启动: $(date '+%H:%M:%S')"
echo "  - 等待开盘: 自动等待到9:30 ET"
echo "  - 结束: 约10:25 ET"
echo ""
echo "⚠️  注意事项:"
echo "  - 开盘第一小时波动极大，注意风控"
echo "  - 建议使用限价单"
echo "  - 10:25会自动清仓"
echo "  - 避免追涨杀跌"
echo ""
echo "💡 优化建议:"
echo "  - 波动大时考虑降低仓位"
echo "  - 关注大盘情绪 (SPY/QQQ)"
echo "  - 注意重要新闻/财报"
echo ""
echo "================================================================================"
echo ""

# 等待进程完成
wait $FIRST_HOUR_PID

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ First hour session completed"
echo ""
echo "查看结果:"
echo "  - 日志: tail logs/first_hour_gemini_*.log"
echo "  - 结果: ls -lh results/*_final_*.json | tail -1"
echo ""
