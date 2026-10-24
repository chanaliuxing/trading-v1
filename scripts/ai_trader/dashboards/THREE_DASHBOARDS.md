# AI Trader 三大屏完整配置指南

## 📊 三大屏总览

AI Trader 提供三个专业监控大屏，覆盖策略进化、系统健康、LLM决策全流程：

| 大屏 | 用途 | 技术栈 | 访问方式 | 核心指标 |
|------|------|--------|----------|----------|
| **大屏一：MLflow** | 策略学习与进化 | MLflow UI | http://localhost:5001 | HPO 参数、RL 性能、A/B 测试 |
| **大屏二：系统实时状态** | 交易员驾驶舱 | Grafana + ClickHouse + Prometheus + Loki | http://localhost:3000/d/hybrid-trading-3ds | 实时 P&L、持仓、系统资源、数据流 |
| **大屏三：AI Brain Monitor** | LLM 决策详情 | Grafana + ClickHouse | http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936 | LLM 会话、决策分布、置信度 |

---

## 🚀 一键启动所有大屏

```bash
cd .
bash scripts/ai_trader/dashboards/open_dashboards.sh
```

这个命令会自动在浏览器中打开所有三个大屏。

---

## 📊 大屏一：MLflow - 策略学习与进化

### 功能说明

MLflow 是策略参数进化的"历史博物馆"，记录每次 `evening_learning` 的成果。

### 核心面板

1. **Experiments 列表**
   - 查看所有 HPO 和 RL 训练记录
   - 实验名称：`AI Trader Evolution`

2. **Metrics 对比**
   - `hpo_oos_sharpe` - 样本外夏普率（HPO 优化目标）
   - `hpo_oos_mdd` - 样本外最大回撤
   - `rl_sharpe` - 强化学习性能
   - `rl_total_reward` - RL 累计奖励

3. **Parameters 查看**
   - 每次 HPO 找到的最优参数
   - 对比不同日期的参数变化

4. **Artifacts 下载**
   - `hpo_decision.json` - HPO 决策文件
   - `daily_report.md` - 每日学习报告
   - `rl_model/` - 强化学习模型文件

### 使用场景

| 场景 | 操作 |
|------|------|
| 查看昨晚学习结果 | 打开 MLflow → 点击最新 run → 查看 Metrics |
| 对比不同日期参数 | 选中多个 run → Compare → Parameters 标签 |
| 筛选优秀实验 | 搜索框输入 `metrics.hpo_oos_sharpe > 1.1` |
| 下载决策文件 | 点击 run → Artifacts → 下载 `hpo_decision.json` |

### 配置文件

- **启动脚本**: `scripts/ai_trader/dashboards/start_mlflow.sh`
- **数据目录**: `./mlruns/`
- **端口**: 5001

### 启动命令

```bash
# 方式1: 使用脚本
bash scripts/ai_trader/dashboards/start_mlflow.sh

# 方式2: 手动启动
cd .
mlflow ui --backend-store-uri file://./mlruns --host 0.0.0.0 --port 5001 &
```

---

## 🖥️ 大屏二：系统实时状态（混合全功能大屏）

### 功能说明

这是交易员的"驾驶舱"，实时展示系统健康状况、P&L、持仓、资源使用。

### 核心面板（共 40 个）

#### 1. 交易员驾驶舱区域

| 面板 | 指标 | 数据源 | 用途 |
|------|------|--------|------|
| **总资产 (Equity)** | 账户总权益 | ClickHouse `trading.account_snapshots` | 实时资产变化 |
| **当日盈亏 (Daily P&L)** | 今日盈亏金额 | ClickHouse `trading.fills` | 盈亏追踪 |
| **可用资金 (Buying Power)** | 可用保证金 | ClickHouse `trading.account_snapshots` | 风险控制 |
| **风险灯 (Margin %)** | 保证金使用率 | 计算字段 | 风险预警 |

#### 2. 持仓监控区域

| 面板 | 指标 | 数据源 | 用途 |
|------|------|--------|------|
| **当前持仓** | 实时持仓列表 | ClickHouse `trading.positions` | 持仓管理 |
| **持仓盈亏** | 未实现盈亏 | ClickHouse `trading.positions` | 风险监控 |
| **持仓分布** | 按标的分布 | ClickHouse `trading.positions` | 组合分析 |

#### 3. 系统资源监控

| 面板 | 指标 | 数据源 | 用途 |
|------|------|--------|------|
| **CPU 使用率** | 系统 CPU % | Prometheus `node_exporter` | 性能监控 |
| **内存使用** | 内存 GB | Prometheus `node_exporter` | 资源预警 |
| **磁盘使用** | 磁盘占用 % | Prometheus `node_exporter` | 存储管理 |
| **网络流量** | 网络 I/O | Prometheus `node_exporter` | 网络健康 |

#### 4. 数据流健康监控

| 面板 | 指标 | 数据源 | 用途 |
|------|------|--------|------|
| **Kafka Lag** | 消息积压量 | Prometheus `kafka_exporter` | 数据管道 |
| **Redis 连接数** | 活跃连接 | Prometheus `redis_exporter` | 缓存健康 |
| **ClickHouse 查询延迟** | 查询耗时 ms | Prometheus `clickhouse_exporter` | 数据库性能 |
| **SFS 1s 延迟** | 1秒 bar 延迟 | Prometheus `sfs_1s_latency` | 实时数据 |

#### 5. 日志与错误

| 面板 | 指标 | 数据源 | 用途 |
|------|------|--------|------|
| **错误日志** | 最近错误 | Loki `{job="trading"}` | 故障排查 |
| **API 调用失败率** | 失败率 % | Prometheus `api_errors_total` | API 健康 |
| **订单拒绝率** | 订单拒绝 % | ClickHouse `trading.orders` | 交易质量 |

### 使用场景

| 场景 | 面板 | 操作 |
|------|------|------|
| 检查今日盈亏 | 当日盈亏 | 查看数值和趋势图 |
| 发现性能瓶颈 | CPU/内存/磁盘 | 观察资源使用率是否超过 80% |
| 数据延迟排查 | SFS 1s 延迟 | 查看延迟是否 > 100ms |
| 查看错误日志 | 错误日志面板 | 搜索关键字（如 "ERROR"） |
| 订单执行监控 | 订单拒绝率 | 确保 < 5% |

### 配置文件

- **Dashboard JSON**: `scripts/ai_trader/dashboards/system_realtime_dashboard.json`
- **访问 URL**: http://localhost:3000/d/hybrid-trading-3ds/e6cabab
- **数据源**:
  - ClickHouse: `host.docker.internal:8123`
  - Prometheus: `http://localhost:9090`
  - Loki: `http://localhost:3100`

### 重要提示

这个大屏是 **实时战报** 的核心，推荐设为主屏幕！

---

## 🧠 大屏三：AI Trader Brain Monitor - LLM 决策详情

### 功能说明

专注于 LLM 决策过程的监控，查看每一个交易决策的详细信息。

### 核心面板（共 6 个）

| 面板 ID | 面板名称 | 指标 | 数据源 | 用途 |
|---------|----------|------|--------|------|
| 1 | **Today's LLM Sessions** | 今日会话总数 | `llm.llm_sessions` | 活动监控 |
| 2 | **LLM Sessions List** | 会话列表（最近20个） | `llm.llm_sessions` | 会话详情 |
| 3 | **LLM Decisions Today** | 今日决策总数 | `llm.llm_decisions` | 决策统计 |
| 4 | **Decision Distribution** | 决策类型分布（饼图） | `llm.llm_decisions` | BUY/SELL/HOLD 比例 |
| 5 | **Decisions by Symbol** | 按标的统计 | `llm.llm_decisions` | 各标的决策详情 |
| 6 | **Recent Decisions** | 最近 20 条决策 | `llm.llm_decisions` | 决策追溯 |

### 关键字段说明

**llm_sessions 表**:
- `session_id` - 会话 ID（UUID）
- `bot_id` - Bot 名称（如 `DEEPSEEK_NVIDIA_DEFAULT`）
- `mode` - 交易模式（`PAPER` / `LIVE`）
- `start_ts_ny` - 开始时间（纽约时区）
- `status` - 状态（`running` / `completed` / `failed`）
- `symbols` - 交易标的列表

**llm_decisions 表**:
- `decision_id` - 决策 ID（UUID）
- `session_id` - 所属会话
- `symbol` - 标的代码
- `ts_ny` - 决策时间（纽约时区）
- `decision_json` - 决策详情（JSON 字符串）
  - `action` - 动作（BUY / SELL / HOLD）
  - `confidence` - 置信度（0-1）
  - `rationale` - 决策理由（文本）
  - `hard_gate` - 硬规则触发（如 `NO_NEW_POSITION`）

### 使用场景

| 场景 | 面板 | 操作 |
|------|------|------|
| 查看今日决策总数 | LLM Decisions Today | 查看统计数值 |
| 分析决策分布 | Decision Distribution | 查看 BUY/SELL/HOLD 比例 |
| 追踪特定标的 | Decisions by Symbol | 查找 TSLA 的决策详情 |
| 查看最近决策理由 | Recent Decisions | 阅读 `rationale` 字段 |
| 排查硬规则触发 | Recent Decisions | 查看 `hard_gate` 是否为 `NO_NEW_POSITION` |

### 配置文件

- **Dashboard JSON**: `scripts/ai_trader/dashboards/ai_trader_dashboard.json`
- **访问 URL**: http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936/ai-trader-brain-monitor
- **数据源**: ClickHouse (`llm` 数据库)
- **刷新频率**: 10 秒

### 时间格式修复

**问题**: 时间显示为 `03:October:42`（月份名称）
**原因**: ClickHouse `formatDateTime()` 中 `%M` = 月份名称
**修复**: 使用 `%i` 表示分钟

```sql
-- 修复前（错误）
formatDateTime(ts_ny, '%H:%M:%S')  -- 显示 03:October:42

-- 修复后（正确）
formatDateTime(ts_ny, '%H:%i:%S')  -- 显示 03:19:42
```

---

## 🛠️ 配置文件清单

```
scripts/ai_trader/dashboards/
├── ai_trader_dashboard.json           # 大屏三：AI Brain Monitor
├── system_realtime_dashboard.json     # 大屏二：系统实时状态
├── clickhouse_datasource.json         # ClickHouse 数据源配置
├── open_dashboards.sh                 # 一键打开三大屏
├── start_mlflow.sh                    # MLflow 启动脚本
├── view_daily_report.sh               # 每日战报查看器
├── GRAFANA_SETUP.md                   # Grafana 配置指南
├── README_DASHBOARDS.md               # 三大屏使用指南
├── QUICK_REFERENCE.md                 # 快速参考卡
└── THREE_DASHBOARDS.md                # 本文档
```

---

## 📖 快速参考

### 一键启动

```bash
# 启动所有基础设施（Docker）
cd .
make up  # 或: cd infra && docker-compose up -d

# 启动 MLflow
bash scripts/ai_trader/dashboards/start_mlflow.sh

# 打开三大屏
bash scripts/ai_trader/dashboards/open_dashboards.sh
```

### 常用 URL

| 服务 | URL |
|------|-----|
| MLflow | http://localhost:5001 |
| Grafana | http://localhost:3000 |
| 系统实时状态 | http://localhost:3000/d/hybrid-trading-3ds/e6cabab |
| AI Brain Monitor | http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936 |
| Prometheus | http://localhost:9090 |
| ClickHouse | http://localhost:8123 |

### 数据库查询

```bash
# ClickHouse - 查看今日 LLM 决策
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM llm.llm_decisions WHERE trading_day = today()"

# ClickHouse - 查看今日盈亏
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT sum(pnl) FROM trading.fills WHERE trading_day = today()"
```

---

## 🐛 故障排查

### 问题1: MLflow 显示 "Access Denied"

**原因**: 端口 5000 被 macOS AirPlay 占用
**解决**: 已改为端口 5001

### 问题2: Grafana 大屏无数据

**检查步骤**:
1. 确认 Docker 服务运行: `docker ps`
2. 检查数据源配置: Grafana → Configuration → Data sources
3. 验证数据存在:
   ```bash
   docker exec trading_clickhouse clickhouse-client --query \
     "SELECT count() FROM llm.llm_sessions WHERE trading_day = today()"
   ```

### 问题3: 系统实时状态大屏缺失部分面板

**原因**: Prometheus 或 Loki 未运行
**解决**:
```bash
# 检查 Prometheus 状态
curl -s http://localhost:9090/-/healthy

# 检查 Loki 状态
curl -s http://localhost:3100/ready

# 重启服务
cd ~/grafana-stack
./stop-stack.sh
./start-stack.sh
```

---

## 📋 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2025-10-29 | 初始版本：整合三大屏配置 |
| 1.1 | 2025-10-29 | 添加系统实时状态大屏（混合全功能） |
| 1.2 | 2025-10-29 | 修复时间格式显示问题 |

---

**最后更新**: 2025-10-29
**维护者**: Trading Team
