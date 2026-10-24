#!/bin/bash
# 盘前交易 (4:00-9:30 ET) - DeepSeek V3
# 运行时间: 5.5小时 (330分钟)

set -e

# 确保在正确的目录
cd "$(dirname "$0")"

echo "================================================================================"
echo "  盘前交易 - DeepSeek V3"
echo "================================================================================"
echo ""
echo "交易时段:"
echo "  - 开始: 4:00 AM ET (盘前)"
echo "  - 结束: 9:25 AM ET (清仓，留5分钟缓冲到开盘)"
echo "  - 总时长: 5.5 小时 (330分钟)"
echo ""
echo "策略配置:"
echo "  - Provider: DeepSeek V3 (快速推理)"
echo "  - Symbols: Universe 15只股票"
echo "  - Interval: 120秒 (每2分钟决策)"
echo "  - Cycles: 165 (5.5小时)"
echo "  - 初始资金: \$50,000"
echo ""
echo "风控设置:"
echo "  - 9:25 强制清仓 (--close-at)"
echo "  - 9:20后禁止开新仓 (--forbid-new-positions-before 5)"
echo ""
echo "================================================================================"
echo ""

# 创建日志目录
mkdir -p logs

# 启动 DeepSeek V3 盘前交易
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Premarket Trading..."

../venv/bin/python run_single_bot.py \
  --provider deepseek \
  --interval 120 \
  --cycles 165 \
  --cash 50000 \
  --prefill-5s-mins 10 \
  --close-at "09:25" \
  --forbid-new-positions-before 5 \
  > logs/premarket_deepseek_$(date '+%Y%m%d_%H%M%S').log 2>&1 &

PREMARKET_PID=$!

echo ""
echo "================================================================================"
echo "盘前交易已启动！"
echo "================================================================================"
echo ""
echo "进程信息:"
echo "  - PID: $PREMARKET_PID"
echo "  - Provider: DeepSeek V3"
echo "  - 运行时长: 330分钟 (165个周期 × 2分钟)"
echo ""
echo "监控命令:"
echo "  - 查看进程: ps aux | grep $PREMARKET_PID"
echo "  - 实时日志: tail -f logs/premarket_deepseek_*.log"
echo "  - 停止交易: kill $PREMARKET_PID"
echo ""
echo "预计完成时间:"
echo "  - 启动: $(date '+%H:%M:%S')"
echo "  - 结束: 约 $(date -v+330M '+%H:%M:%S' 2>/dev/null || date -d '+330 minutes' '+%H:%M:%S' 2>/dev/null || echo '9:25 ET (约5.5小时后)')"
echo ""
echo "⚠️  注意事项:"
echo "  - 盘前流动性低，价差可能较大"
echo "  - 建议使用限价单，避免滑点"
echo "  - 9:25会自动清仓，准备开盘"
echo ""
echo "================================================================================"
echo ""

# 等待进程完成
wait $PREMARKET_PID

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Premarket session completed"
echo ""
echo "查看结果:"
echo "  - 日志: tail logs/premarket_deepseek_*.log"
echo "  - 结果: ls -lh results/*_final_*.json | tail -1"
echo ""
