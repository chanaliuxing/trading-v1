#!/bin/bash
# AI Trader 三大屏快捷打开脚本
# 一键在浏览器中打开所有大屏

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 正在打开 AI Trader 三大屏..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 1. MLflow - 策略学习与进化
echo "📊 [1/3] 打开 MLflow (策略学习与进化)..."
if command -v open &> /dev/null; then
    open http://localhost:5001
elif command -v xdg-open &> /dev/null; then
    xdg-open http://localhost:5001
else
    echo "   请手动打开: http://localhost:5001"
fi
sleep 1

# 2. Grafana - 系统实时状态 (基础设施健康监控)
echo "🖥️  [2/3] 打开 Grafana 系统实时状态 (CPU/内存/磁盘/Prometheus监控)..."
if command -v open &> /dev/null; then
    open "http://localhost:3000/d/infra-platform-health/infrastructure-and-platform-health"
elif command -v xdg-open &> /dev/null; then
    xdg-open "http://localhost:3000/d/infra-platform-health/infrastructure-and-platform-health"
else
    echo "   请手动打开: http://localhost:3000/d/infra-platform-health/infrastructure-and-platform-health"
fi
sleep 1

# 3. Grafana - AI Trader Brain Monitor (LLM决策监控)
echo "🧠 [3/3] 打开 Grafana AI Trader Brain Monitor (LLM决策详情)..."
if command -v open &> /dev/null; then
    open "http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936/ai-trader-brain-monitor"
elif command -v xdg-open &> /dev/null; then
    xdg-open "http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936/ai-trader-brain-monitor"
else
    echo "   请手动打开: http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 三大屏已在浏览器中打开！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📋 查看每日战报，请运行:"
echo "   bash scripts/ai_trader/dashboards/view_daily_report.sh"
echo ""
echo "💡 提示: 推荐将【混合全功能大屏】设为主屏，它包含:"
echo "   • 实时P&L和持仓"
echo "   • 系统资源监控 (CPU/内存/磁盘)"
echo "   • 数据流健康状况"
echo "   • 订单执行情况"
echo ""
