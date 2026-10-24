# Grafana 快速启动指南

## 🚀 一键启动

```bash
# 启动 Grafana (如果还没运行)
cd ./infra
docker compose up -d grafana

# 等待 10 秒让 Grafana 完全启动
sleep 10

# 访问 Dashboard
open http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
```

**默认登录**:
- Username: `admin`
- Password: `admin`

---

## 📊 主要 Dashboards

### 1. AI Trader Complete Dashboard (推荐) ⭐
**包含所有 21 个监控指标**

直接访问: http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics

**监控内容**:
- 📈 性能指标: OOS Sharpe, MDD, Trades
- ✅ 数据质量: DQ Pass/Fail
- 🛡️ 风险控制: Guardian, Hedging
- 🤖 策略部署: Pending Actions
- 💼 组合管理: Overlay Beta
- 🔧 工具健康: Kill/Restart/Backoff
- 🧠 Brain Calls: LLM 调用追踪

---

### 2. Trading System Operations
**实时交易系统运行状态**

访问: http://localhost:3000/d/trading-system-ops/trading-system-operations

---

### 3. Strategy & Decision Pipeline
**策略和决策流水线监控**

访问: http://localhost:3000/d/strategy-pipeline/strategy-and-decision-pipeline-insight

---

### 4. Financial & Market Overview
**金融市场总览**

访问: http://localhost:3000/d/financial-market-overview/financial-and-market-overview

---

### 5. Infrastructure & Platform Health
**基础设施健康度**

访问: http://localhost:3000/d/infra-platform-health/infrastructure-and-platform-health

---

## 🔍 如果看到 "No data"

### 原因 1: 今天还没有运行 AI Trader
**解决**: 启动 AI Trader
```bash
cd .
python scripts/ai_trader/auto_loop.py
```

### 原因 2: 时间范围设置不对
**解决**: 在 Dashboard 右上角调整时间范围
- 点击右上角的时间选择器
- 选择 "Last 7 days" 或 "Last 30 days"
- 点击 "Apply"

### 原因 3: ClickHouse 没有运行
**检查**:
```bash
docker ps | grep clickhouse
```

**如果没运行,启动**:
```bash
cd ./infra
docker compose up -d clickhouse
```

---

## 🛠️ 常用命令

### 查看 Grafana 状态
```bash
docker ps | grep grafana
```

### 重启 Grafana
```bash
docker restart trading_grafana
```

### 查看 Grafana 日志
```bash
docker logs trading_grafana -f
```

### 停止 Grafana
```bash
docker stop trading_grafana
```

### 启动所有服务
```bash
cd .
make up
```

---

## 📱 Dashboard 功能

### 自动刷新
- 默认每 30 秒自动刷新
- 可在右上角调整刷新间隔

### 时间范围
- 默认: Last 7 days
- 可选: Last 6h, 12h, 24h, 7d, 30d, 90d

### 面板操作
- **查看详情**: 点击面板标题 → View
- **编辑查询**: 点击面板标题 → Edit
- **全屏**: 点击面板标题 → View → 点击右上角全屏按钮
- **导出数据**: 点击面板标题 → More → Export data

### 分享 Dashboard
- 点击右上角 "Share" 按钮
- 选择 "Link" 或 "Snapshot"
- 复制 URL

---

## 🔐 安全提示

**生产环境建议**:
1. 修改默认密码:
   - 登录后点击用户头像 → Profile → Change Password
2. 创建新用户:
   - Configuration → Users → New User
3. 设置访问权限:
   - Configuration → Permissions

**当前配置** (开发环境):
- Username: `admin`
- Password: `admin`
- ⚠️ 仅用于本地开发

---

## 📊 数据说明

### 数据来源
- **ClickHouse**: ai_trader_dash_metrics, ai_trader_events
- **时区**: America/New_York (EST/EDT)

### 指标更新
- **性能指标**: 每次 backtest 后
- **质量指标**: 实时 (每个 bar)
- **Brain Calls**: 实时 (每次 LLM 调用)
- **工具事件**: 实时 (每次工具操作)

---

## 🎯 推荐使用流程

### 1. 每日启动检查
```bash
# 1. 启动所有服务
cd . && make up

# 2. 等待服务启动
sleep 30

# 3. 打开主 Dashboard
open http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics

# 4. 启动 AI Trader
python scripts/ai_trader/auto_loop.py
```

### 2. 监控要点
- ✅ **OOS Sharpe > 1.0**: 策略表现良好
- ✅ **DQ Failed = 0**: 数据质量正常
- ✅ **Guardian Max DD < -5%**: 风险控制生效
- ✅ **Tools Running = 1**: LLM 工具正常
- ⚠️ **Pending Count > 0**: 有待处理的建议

### 3. 异常响应
如果看到:
- **OOS Sharpe < 0.5**: 检查策略参数
- **DQ Failed > 0**: 检查数据源
- **Guardian Max DD < -10%**: 考虑停止交易
- **Tools Running = 0**: 重启 orchestrator
- **Brain Success Rate < 90%**: 检查 LLM 配置

---

## 📞 获取帮助

### 查看完整文档
```bash
cat ./grafana/GRAFANA_RECONFIGURATION_COMPLETE.md
```

### 查看指标说明
```bash
cat ./scripts/ai_trader/dashboards/打点清单.md
```

### 查看 Brain 监控架构
```bash
cat ./doc/AI_BRAIN_TACTICAL_MANUAL.md
```

---

**快速链接**:
- Grafana UI: http://localhost:3000
- Complete Dashboard: http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
- ClickHouse UI: http://localhost:8123/play

🎉 **开始监控你的 AI Trader 吧!**
