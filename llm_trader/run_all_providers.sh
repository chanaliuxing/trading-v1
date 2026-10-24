#!/bin/bash
# 并发运行所有 LLM Provider
# DeepSeek R1 (NVIDIA) 使用 180 秒间隔（推理模型，反应慢）

set -e

echo "================================================================================"
echo "  多 Provider 并发测试"
echo "================================================================================"
echo ""
echo "启动配置:"
echo "  - Symbols: From config.yml universe (15 symbols)"
echo "  - Cycles: 10"
echo "  - Prefill: 10 minutes"
echo ""
echo "Interval 配置:"
echo "  - openai:          60s  (快速)"
echo "  - deepseek:        65s  (快速)"
echo "  - gemini:          70s  (快速)"
echo "  - deepseek_nvidia: 180s (推理模型，最多等 180 秒)"
echo ""
echo "================================================================================"
echo ""

# 创建日志目录
mkdir -p logs

# 确保在正确的目录
cd "$(dirname "$0")"

# 启动所有 provider
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting OpenAI..."
../venv/bin/python run_single_bot.py \
  --provider openai \
  --cycles 10 \
  --interval 60 \
  --prefill-5s-mins 10 \
  > logs/openai_$(date '+%Y%m%d_%H%M%S').log 2>&1 &
OPENAI_PID=$!
echo "  → PID: $OPENAI_PID"
sleep 2

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting DeepSeek..."
../venv/bin/python run_single_bot.py \
  --provider deepseek \
  --cycles 10 \
  --interval 65 \
  --prefill-5s-mins 10 \
  > logs/deepseek_$(date '+%Y%m%d_%H%M%S').log 2>&1 &
DEEPSEEK_PID=$!
echo "  → PID: $DEEPSEEK_PID"
sleep 2

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Gemini..."
../venv/bin/python run_single_bot.py \
  --provider gemini \
  --cycles 10 \
  --interval 70 \
  --prefill-5s-mins 10 \
  > logs/gemini_$(date '+%Y%m%d_%H%M%S').log 2>&1 &
GEMINI_PID=$!
echo "  → PID: $GEMINI_PID"
sleep 2

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting DeepSeek-NVIDIA (R1 推理模型)..."
echo "  ⚠️  注意: R1 是推理模型，每次决策可能需要 15-30 秒"
echo "  ⚠️  Interval 设置为 180 秒，确保有足够时间完成推理"
../venv/bin/python run_single_bot.py \
  --provider deepseek_nvidia \
  --cycles 10 \
  --interval 180 \
  --prefill-5s-mins 10 \
  > logs/deepseek_nvidia_$(date '+%Y%m%d_%H%M%S').log 2>&1 &
NVIDIA_PID=$!
echo "  → PID: $NVIDIA_PID"

echo ""
echo "================================================================================"
echo "所有 Provider 已启动！"
echo "================================================================================"
echo ""
echo "进程列表:"
echo "  - OpenAI:          PID $OPENAI_PID"
echo "  - DeepSeek:        PID $DEEPSEEK_PID"
echo "  - Gemini:          PID $GEMINI_PID"
echo "  - DeepSeek-NVIDIA: PID $NVIDIA_PID"
echo ""
echo "监控命令:"
echo "  - 查看进程:     ps aux | grep run_single_bot"
echo "  - 实时日志:     tail -f logs/*.log"
echo "  - 查看结果:     ls -lh results/*_final_*.json"
echo "  - 停止所有:     kill $OPENAI_PID $DEEPSEEK_PID $GEMINI_PID $NVIDIA_PID"
echo ""
echo "等待所有 Provider 完成..."
echo "================================================================================"
echo ""

# 等待所有进程完成
wait $OPENAI_PID
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ OpenAI completed"

wait $DEEPSEEK_PID
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ DeepSeek completed"

wait $GEMINI_PID
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Gemini completed"

wait $NVIDIA_PID
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ DeepSeek-NVIDIA completed"

echo ""
echo "================================================================================"
echo "所有 Provider 测试完成！"
echo "================================================================================"
echo ""
echo "结果文件:"
ls -lh results/*_final_*.json | tail -4
echo ""
echo "查看详细日志:"
echo "  tail logs/*.log"
echo ""
