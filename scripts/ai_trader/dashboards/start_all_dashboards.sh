#!/bin/bash
# AI Trader 三大屏统一启动脚本
# 一键启动 MLflow、Grafana 和每日战报查看器

set -euo pipefail

PROJECT_ROOT="."
cd "$PROJECT_ROOT"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 AI Trader 三大屏统一启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📊 将启动以下服务："
echo "   1️⃣  MLflow UI         - 策略学习与进化仪表盘 (端口 5000)"
echo "   2️⃣  Grafana          - 系统实时状态监控 (端口 3000)"
echo "   3️⃣  Daily Report     - 每日战报查看器 (交互式)"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 检查基础设施是否运行
echo "🔍 检查基础设施状态..."
echo ""

# 检查 Docker 服务
if ! docker ps &> /dev/null; then
    echo "❌ Docker 未运行！"
    echo "   请先启动 Docker Desktop"
    exit 1
fi

# 检查必要的容器
required_containers=("trading_clickhouse" "trading_prometheus" "trading_grafana")
missing_containers=()

for container in "${required_containers[@]}"; do
    if ! docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
        missing_containers+=("$container")
    fi
done

if [ ${#missing_containers[@]} -gt 0 ]; then
    echo "⚠️  以下容器未运行："
    for container in "${missing_containers[@]}"; do
        echo "   - $container"
    done
    echo ""
    echo "正在启动基础设施..."
    cd "$PROJECT_ROOT/infra"
    docker-compose up -d
    echo "✅ 基础设施已启动，等待 10 秒初始化..."
    sleep 10
    cd "$PROJECT_ROOT"
else
    echo "✅ 所有基础设施容器已运行"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. 启动 MLflow
echo ""
echo "📊 [1/3] 启动 MLflow UI..."
echo ""

if lsof -Pi :5000 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "   ✅ MLflow 已在运行 (http://localhost:5000)"
else
    bash scripts/ai_trader/dashboards/start_mlflow.sh > /tmp/mlflow_startup.log 2>&1 &
    MLFLOW_PID=$!
    echo "   🔄 MLflow 正在启动... (PID: $MLFLOW_PID)"
    sleep 3
    if lsof -Pi :5000 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        echo "   ✅ MLflow 已就绪 (http://localhost:5000)"
    else
        echo "   ⚠️  MLflow 启动中，请稍候访问"
    fi
fi

# 2. 检查 Grafana
echo ""
echo "📈 [2/3] 检查 Grafana..."
echo ""

if curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
    echo "   ✅ Grafana 已就绪 (http://localhost:3000)"
    echo "   📖 默认登录: admin / admin"
else
    echo "   ⚠️  Grafana 未响应，正在检查..."
    if docker ps --format '{{.Names}}' | grep -q "trading_grafana"; then
        echo "   🔄 Grafana 容器运行中，等待初始化..."
        sleep 5
        if curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
            echo "   ✅ Grafana 已就绪"
        else
            echo "   ❌ Grafana 无法访问，请检查日志: docker logs trading_grafana"
        fi
    else
        echo "   ❌ Grafana 容器未运行"
    fi
fi

# 3. 配置 Grafana 数据源（如果需要）
echo ""
echo "🔧 [3/3] 配置 Grafana..."
echo ""

# 检查 ClickHouse 数据源是否已配置
if curl -s -u admin:admin http://localhost:3000/api/datasources | grep -q '"name":"ClickHouse"'; then
    echo "   ✅ ClickHouse 数据源已配置"
else
    echo "   ⚠️  ClickHouse 数据源未配置，正在添加..."
    cat > /tmp/clickhouse_datasource.json <<EOF
{
  "name": "ClickHouse",
  "type": "vertamedia-clickhouse-datasource",
  "url": "http://host.docker.internal:8123",
  "access": "proxy",
  "jsonData": {
    "defaultDatabase": "llm",
    "usePOST": true
  },
  "isDefault": false
}
EOF
    if curl -s -X POST -H "Content-Type: application/json" -u admin:admin \
        http://localhost:3000/api/datasources \
        -d @/tmp/clickhouse_datasource.json > /dev/null 2>&1; then
        echo "   ✅ ClickHouse 数据源已添加"
    else
        echo "   ⚠️  ClickHouse 数据源添加失败（可能已存在或需要手动配置）"
    fi
fi

# 导入 AI Trader 仪表盘
echo ""
echo "📥 导入 AI Trader 仪表盘..."
echo ""

DASHBOARD_FILE="$PROJECT_ROOT/grafana/dashboards/active/ai_trader_brain_monitor.json"
if [ -f "$DASHBOARD_FILE" ]; then
    if curl -s -X POST -H "Content-Type: application/json" -u admin:admin \
        http://localhost:3000/api/dashboards/db \
        -d @"$DASHBOARD_FILE" > /dev/null 2>&1; then
        echo "   ✅ AI Trader 仪表盘已导入"
    else
        echo "   ⚠️  仪表盘导入失败（可能已存在）"
    fi
else
    echo "   ⚠️  仪表盘文件不存在: $DASHBOARD_FILE"
fi

# 总结
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 所有服务已就绪！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "🌐 快速访问："
echo ""
echo "   📊 大屏一：MLflow（策略学习与进化）"
echo "      http://localhost:5000"
echo "      功能：HPO 参数进化、RL 性能趋势、A/B 测试对比"
echo ""
echo "   📈 大屏二：Grafana（系统实时状态）"
echo "      http://localhost:3000 (admin/admin)"
echo "      仪表盘：AI Trader Brain - Real-Time Monitor"
echo "      功能：实时 P&L、LLM 决策分布、进程心跳、API 错误"
echo ""
echo "   📋 大屏三：每日战报（高层总结）"
echo "      运行: bash scripts/ai_trader/dashboards/view_daily_report.sh"
echo "      功能：今日诊断、处方、HPO/RL 结果、守护者活动"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💡 快捷命令："
echo ""
echo "   查看所有服务状态:"
echo "   docker ps --filter name=trading_"
echo ""
echo "   查看 MLflow 日志:"
echo "   tail -f /tmp/mlflow_startup.log"
echo ""
echo "   查看 Grafana 日志:"
echo "   docker logs -f trading_grafana"
echo ""
echo "   停止所有服务:"
echo "   pkill -f 'mlflow ui' && cd infra && docker-compose down"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 提示：是否现在查看每日战报
echo -n "🤔 是否现在查看每日战报？ [y/N]: "
read -r view_report

if [[ "$view_report" =~ ^[Yy]$ ]]; then
    echo ""
    bash scripts/ai_trader/dashboards/view_daily_report.sh
else
    echo ""
    echo "👋 稍后可运行查看："
    echo "   bash scripts/ai_trader/dashboards/view_daily_report.sh"
fi

echo ""
echo "🎉 Happy Trading!"
