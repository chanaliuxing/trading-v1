#!/bin/bash
# MLflow 启动脚本 - AI Trader 策略学习与进化仪表盘
# 用途：展示 HPO、RL、策略参数进化、性能趋势

set -euo pipefail

PROJECT_ROOT="."
MLRUNS_DIR="${PROJECT_ROOT}/mlruns"
MLFLOW_PORT=5001  # Changed from 5000 (conflicts with macOS AirPlay)

# 确保 mlruns 目录存在
mkdir -p "$MLRUNS_DIR"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 启动 MLflow UI - AI Trader 策略学习与进化仪表盘"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📊 功能说明："
echo "   1. 策略性能趋势：OOS 夏普率、最大回撤等核心指标"
echo "   2. 参数进化：每次 HPO 找到的最优参数"
echo "   3. 决策归档：hpo_decision.json, daily_report.md 等"
echo "   4. 实验对比：Champion vs Challenger A/B 测试"
echo ""
echo "📁 数据目录: $MLRUNS_DIR"
echo "🌐 访问地址: http://localhost:${MLFLOW_PORT}"
echo "   ⚠️  注意：端口 5000 被 macOS AirPlay 占用，使用 5001"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 检查 MLflow 是否安装
if ! command -v mlflow &> /dev/null; then
    echo "❌ MLflow 未安装！正在安装..."
    pip install mlflow --quiet
    echo "✅ MLflow 安装完成"
fi

# 检查端口是否被占用
if lsof -Pi :${MLFLOW_PORT} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "⚠️  端口 ${MLFLOW_PORT} 已被占用"
    echo "   如需重启，请先执行: pkill -f 'mlflow ui'"
    echo ""
    echo "   已有服务仍在运行，访问: http://localhost:${MLFLOW_PORT}"
    exit 0
fi

# 启动 MLflow UI
cd "$PROJECT_ROOT"
echo "🔄 正在启动 MLflow UI..."
echo ""

mlflow ui \
    --backend-store-uri "file://${MLRUNS_DIR}" \
    --host 0.0.0.0 \
    --port ${MLFLOW_PORT} \
    &

MLFLOW_PID=$!
echo "✅ MLflow UI 已启动 (PID: ${MLFLOW_PID})"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📖 使用指南："
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1️⃣  打开浏览器访问: http://localhost:${MLFLOW_PORT}"
echo ""
echo "2️⃣  查看实验："
echo "   - 点击 \"AI Trader Evolution\" 实验"
echo "   - 查看所有运行记录（每次 evening_learning 都会创建一个 run）"
echo ""
echo "3️⃣  关键指标："
echo "   - hpo_oos_sharpe: 样本外夏普率（HPO 优化目标）"
echo "   - hpo_oos_mdd: 样本外最大回撤"
echo "   - hpo_is_profit: 样本内总收益"
echo "   - rl_sharpe: RL 模型夏普率"
echo "   - ab_champion_sharpe: Champion 策略夏普率"
echo ""
echo "4️⃣  查看参数："
echo "   - 点击 run → Parameters 查看该次运行的所有参数"
echo "   - 对比不同 run 的参数差异"
echo ""
echo "5️⃣  查看决策文件："
echo "   - 点击 run → Artifacts"
echo "   - 下载 hpo_decision.json / daily_report.md 等文件"
echo ""
echo "6️⃣  停止服务："
echo "   pkill -f 'mlflow ui'"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💡 提示："
echo "   - 如果没有数据，请先运行 AI Trader evening_learning 生成记录"
echo "   - 命令: PYTHONPATH=. python scripts/ai_trader/cli.py run --mode learn"
echo ""

# 等待用户按 Ctrl+C
echo "🟢 服务运行中，按 Ctrl+C 停止..."
wait $MLFLOW_PID
