# AI Trader 三大屏部署总结

## ✅ 部署完成时间
**2025-10-29**

---

## 📊 已部署的三大屏

### 1️⃣ **MLflow - 策略学习与进化仪表盘**

**状态**: ✅ 已部署并运行

**访问地址**: http://localhost:5000

**功能**:
- 策略性能趋势（OOS 夏普率、最大回撤）
- 参数进化追踪（每次 HPO 的最优参数）
- 决策归档（hpo_decision.json、daily_report.md）
- 实验对比（Champion vs Challenger A/B 测试）

**启动方式**:
```bash
bash scripts/ai_trader/dashboards/start_mlflow.sh
```

**数据来源**:
- `orchestrator.py` 中的 `evening_learning` 方法自动打点
- 存储路径: `./mlruns/`

---

### 2️⃣ **Grafana - 系统实时状态监控**

**状态**: ✅ 已部署并运行

**访问地址**: http://localhost:3000

**默认登录**: admin / admin

**仪表盘名称**: "AI Trader Brain - Real-Time Monitor"

**核心面板**:
1. AI Brain Status (今日会话数量)
2. LLM Sessions Today (会话列表)
3. Today's P&L by Symbol (盈亏曲线)
4. LLM Decision Distribution (决策分布)
5. Process Heartbeats (进程心跳)
6. LLM API Errors (API 错误率)
7. Brain Decisions Log (决策日志)
8. Strategy Performance (策略表现)

**数据来源**:
- **ClickHouse**: `llm.llm_sessions`, `llm.llm_decisions`, `llm.llm_fills`
- **Prometheus**: 进程心跳、API 错误计数器
- **Loki**: 日志流（可选）

**仪表盘文件**: `grafana/dashboards/active/ai_trader_brain_monitor.json`

---

### 3️⃣ **Daily Report - 每日战报查看器**

**状态**: ✅ 已部署

**启动命令**:
```bash
bash scripts/ai_trader/dashboards/view_daily_report.sh
```

**功能**:
- 交互式菜单（查看今日/历史战报）
- 浏览器打开（HTML 版本）
- 显示相关文件（HPO/RL 决策）
- 最近 5 天汇总

**数据来源**:
- `report_builder.py` 生成的 `daily_report.md`
- 存储路径: `runs/ai_trader/YYYY-MM-DD/daily_report.md`

**增强工具**（可选）:
- `glow` - Markdown 终端渲染器
- `bat` - 带高亮的 cat
- `pandoc` - Markdown 转 HTML

---

## 🚀 统一启动

### 一键启动所有大屏

```bash
cd .
bash scripts/ai_trader/dashboards/start_all_dashboards.sh
```

**此脚本会自动**:
1. ✅ 检查 Docker 基础设施
2. ✅ 启动 MLflow UI
3. ✅ 验证 Grafana 可访问
4. ✅ 配置 Grafana 数据源和仪表盘
5. ✅ 提供每日战报查看选项

---

## 📁 创建的文件清单

### 启动脚本
```
scripts/ai_trader/dashboards/
├── start_mlflow.sh                 # MLflow 启动脚本
├── view_daily_report.sh            # 每日战报查看器
├── start_all_dashboards.sh         # 统一启动脚本（三合一）
```

### 配置文件
```
grafana/dashboards/active/
└── ai_trader_brain_monitor.json    # Grafana AI Trader 仪表盘
```

### 文档
```
scripts/ai_trader/dashboards/
├── README_DASHBOARDS.md            # 完整使用指南（8000+ 字）
├── QUICK_REFERENCE.md              # 快速参考卡片
└── DEPLOYMENT_SUMMARY.md           # 部署总结（本文件）
```

---

## 🔧 依赖与环境

### Python 包
- ✅ `mlflow` (已安装 v3.5.1)

### Docker 容器
- ✅ `trading_clickhouse` (运行中)
- ✅ `trading_prometheus` (运行中)
- ✅ `trading_grafana` (运行中)
- ✅ `trading_redis` (运行中)
- ✅ `trading_kafka` (运行中)

### 可选工具（增强体验）
- `glow` - Markdown 终端渲染器 (`brew install glow`)
- `bat` - 带高亮的 cat (`brew install bat`)
- `pandoc` - Markdown 转 HTML (`brew install pandoc`)

---

## 📊 数据流

```
┌─────────────────────────────────────────────────────────────┐
│ evening_learning (每晚 20:00-次日 4:00)                     │
│  ├─ HPO 优化                                                 │
│  ├─ RL 训练                                                  │
│  ├─ A/B 测试评估                                             │
│  └─ 生成决策文件                                             │
└─────────────────────────────────────────────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
         ▼                ▼                ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   MLflow    │  │   Grafana   │  │ Daily Report│
│   (5000)    │  │   (3000)    │  │   (Shell)   │
│             │  │             │  │             │
│ • Metrics   │  │ • Sessions  │  │ • Summary   │
│ • Params    │  │ • P&L       │  │ • Decisions │
│ • Artifacts │  │ • Heartbeat │  │ • Actions   │
└─────────────┘  └─────────────┘  └─────────────┘
       │                 │
       │                 │
       ▼                 ▼
┌─────────────┐  ┌─────────────┐
│  mlruns/    │  │ ClickHouse  │
│             │  │  llm.*      │
└─────────────┘  └─────────────┘
```

---

## 🎯 典型使用场景

### 场景 1：早晨开盘前检查

**目标**: 确认系统状态、查看昨晚学习结果

```bash
# 1. 启动所有大屏
bash scripts/ai_trader/dashboards/start_all_dashboards.sh

# 2. 打开 MLflow 查看 HPO 结果
open http://localhost:5000

# 3. 查看每日战报
# (在 start_all_dashboards.sh 交互界面选择 [y])

# 4. 打开 Grafana 实时监控
open http://localhost:3000
```

### 场景 2：盘中实时监控

**目标**: 监控 LLM 决策质量、P&L 变化、API 健康状态

```bash
# 1. Grafana 设置自动刷新 (10s)
# 浏览器: http://localhost:3000 → AI Trader Brain 仪表盘 → 右上角刷新按钮

# 2. 全屏展示（按 F 键）

# 3. 关注关键面板：
#    - Today's P&L by Symbol (盈亏曲线)
#    - LLM API Errors (错误率)
#    - Process Heartbeats (心跳延迟)
```

### 场景 3：盘后复盘

**目标**: 分析今日表现、查看决策质量

```bash
# 1. 查看每日战报
bash scripts/ai_trader/dashboards/view_daily_report.sh

# 2. 选择 [1] 查看今日战报

# 3. 选择 [4] 显示相关文件
#    - hpo_decision.json
#    - rl_decision.json
#    - reflection.json

# 4. 在 Grafana 查看 Strategy Performance 表格
#    - 各 symbol 决策统计
#    - 平均信心度
#    - 总 P&L
```

### 场景 4：晚间学习后检查

**目标**: 确认 HPO/RL 是否成功、新参数是否优秀

```bash
# 1. 打开 MLflow
open http://localhost:5000

# 2. 筛选最新实验
#    - 按 Start Time 降序排列
#    - 查看 hpo_oos_sharpe 指标

# 3. 对比新旧参数
#    - 选中最新 run 和昨天的 run
#    - 点击 Compare → Parameters

# 4. 决定是否应用新参数
#    - 如果 OOS Sharpe > 1.1 且 MDD < 0.1
#    - 新参数已自动应用（参考 daily_report.md）
```

---

## 🛠️ 维护任务

### 每日任务
- ✅ 查看 Grafana 确认系统运行正常
- ✅ 查看每日战报了解策略调整

### 每周任务
- 📊 MLflow 清理旧实验（保留最近 90 天）
- 📊 Grafana 备份仪表盘配置
- 📊 ClickHouse 检查磁盘使用率

### 每月任务
- 🔧 更新 Grafana 插件
- 🔧 优化 ClickHouse 表（OPTIMIZE TABLE）
- 🔧 审查告警规则（是否需要调整阈值）

---

## 📚 相关文档

### 主要文档
- **完整指南**: `scripts/ai_trader/dashboards/README_DASHBOARDS.md`
- **快速参考**: `scripts/ai_trader/dashboards/QUICK_REFERENCE.md`
- **监控指南**: `scripts/ai_trader/MONITORING_GUIDE.md`
- **快速启动**: `scripts/ai_trader/QUICK_START.md`

### 系统设计
- **系统设计**: `scripts/ai_trader/SYSTEM_DESIGN.md`
- **使用指南**: `scripts/ai_trader/HOW_TO_USE.md`

---

## 🎉 成果总结

### 实现的功能
✅ **大屏一 (MLflow)**: 策略学习与进化可视化
✅ **大屏二 (Grafana)**: 实时系统状态监控
✅ **大屏三 (Daily Report)**: 每日战报自动化查看
✅ **统一启动脚本**: 一键启动所有服务
✅ **完整文档**: 8000+ 字使用指南

### 创建的文件
- 3 个启动脚本
- 1 个 Grafana 仪表盘配置
- 3 个文档文件

### 安装的依赖
- MLflow 3.5.1
- 已有 Docker 基础设施（ClickHouse、Prometheus、Grafana）

### 验证状态
✅ MLflow 运行在端口 5000
✅ Grafana 运行在端口 3000 并健康（version 12.2.1）
✅ ClickHouse 数据库连接正常
✅ 所有脚本可执行权限已设置

---

## 🚀 下一步建议

1. **配置告警通知**
   - 在 Grafana 中添加 Slack/Email 通知渠道
   - 配置关键告警规则（API 错误率、心跳超时等）

2. **定时任务**
   - 设置 cron 自动运行 `evening_learning`
   - 自动生成和发送每日战报

3. **增强工具**
   - 安装 `glow` 增强 Markdown 阅读体验
   - 安装 `pandoc` 生成专业 HTML 报告

4. **备份策略**
   - 定期备份 MLflow 实验数据
   - 导出 Grafana 仪表盘配置
   - 归档历史每日战报

5. **性能优化**
   - 配置 ClickHouse TTL 清理旧数据
   - 限制 Grafana 查询时间范围
   - 定期 OPTIMIZE ClickHouse 表

---

**部署完成日期**: 2025-10-29
**维护者**: Trading Team
**版本**: 1.0
