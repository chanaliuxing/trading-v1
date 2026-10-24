# Grafana Dashboard 配置指南

本目录包含 AI Trader 的 Grafana 监控大屏配置文件。

## 📁 配置文件

- `clickhouse_datasource.json` - ClickHouse 数据源配置
- `ai_trader_dashboard.json` - AI Trader Brain Monitor 大屏配置

## 🚀 快速部署

### 1. 配置 ClickHouse 数据源

```bash
# 导入数据源配置（仅首次需要）
curl -X POST -u admin:admin \
  -H "Content-Type: application/json" \
  -d @clickhouse_datasource.json \
  http://localhost:3000/api/datasources
```

**数据源配置说明：**
- **名称**: ClickHouse
- **类型**: grafana-clickhouse-datasource
- **服务器**: host.docker.internal (从 Docker 容器访问宿主机)
- **端口**: 8123
- **默认数据库**: llm

### 2. 导入大屏

```bash
# 导入 AI Trader 大屏
curl -X POST -u admin:admin \
  -H "Content-Type: application/json" \
  -d @ai_trader_dashboard.json \
  http://localhost:3000/api/dashboards/db
```

导入成功后，输出会包含 dashboard URL：
```json
{
  "uid": "5ff12312-7742-4d51-81a9-35eacbf30936",
  "url": "/d/5ff12312-7742-4d51-81a9-35eacbf30936/ai-trader-brain-monitor"
}
```

### 3. 访问大屏

```bash
# 方式1: 使用快捷脚本
bash open_dashboards.sh

# 方式2: 直接访问 URL
open http://localhost:3000/d/5ff12312-7742-4d51-81a9-35eacbf30936/ai-trader-brain-monitor
```

## 📊 大屏面板说明

### Panel 1: Today's LLM Sessions (统计面板)
- **指标**: 今日 LLM 会话总数
- **数据源**: `llm.llm_sessions` 表
- **阈值**:
  - 红色: 0 会话（无活动）
  - 黄色: 1-4 会话（低活动）
  - 绿色: ≥5 会话（正常活动）

### Panel 2: LLM Sessions List (表格)
- **内容**: 最近 20 个会话列表
- **字段**:
  - session (会话ID前8位)
  - bot_id (Bot名称)
  - mode (交易模式: PAPER/LIVE)
  - started (开始时间 HH:mm:SS)
  - status (状态)
  - symbols (交易标的)

### Panel 3: LLM Decisions Today (统计面板)
- **指标**: 今日决策总数
- **数据源**: `llm.llm_decisions` 表

### Panel 4: Decision Distribution (饼图)
- **内容**: 决策类型分布
- **类型**: BUY / SELL / HOLD
- **数据源**: `decision_json` 字段解析

### Panel 5: Decisions by Symbol (表格)
- **内容**: 各标的决策统计
- **字段**:
  - symbol (标的)
  - total (总决策数)
  - buys (买入次数)
  - sells (卖出次数)
  - holds (持有次数)
  - avg_conf (平均置信度)

### Panel 6: Recent Decisions (表格)
- **内容**: 最近 20 条决策详情
- **字段**:
  - time (时间 HH:mm:SS)
  - symbol (标的)
  - action (动作: BUY/SELL/HOLD)
  - confidence (置信度 0-1)
  - rationale (决策理由，截取前100字符)

## 🔧 时间格式修复说明

**问题**: 原始配置使用 `formatDateTime(ts_ny, '%H:%M:%S')` 导致分钟显示为月份名称（例如：`03:October:42`）

**原因**: ClickHouse 的 `formatDateTime()` 函数中 `%M` 表示月份名称，而非分钟

**修复**: 改为 `formatDateTime(ts_ny, '%H:%i:%S')`（`%i` 表示分钟）

**影响面板**:
- Panel 2: LLM Sessions List (started 字段)
- Panel 6: Recent Decisions (time 字段)

## 🔄 更新大屏配置

如果修改了 `ai_trader_dashboard.json`，重新导入即可覆盖：

```bash
curl -X POST -u admin:admin \
  -H "Content-Type: application/json" \
  -d @ai_trader_dashboard.json \
  http://localhost:3000/api/dashboards/db
```

**注意**: 设置了 `"overwrite": true`，会自动覆盖同名 dashboard。

## 📖 相关文档

- [README_DASHBOARDS.md](./README_DASHBOARDS.md) - 三大屏使用指南
- [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - 快速参考卡
- [DEPLOYMENT_SUMMARY.md](./DEPLOYMENT_SUMMARY.md) - 部署总结

## 🐛 故障排查

### 问题1: 数据源连接失败
```
Error: invalid server host. Either empty or not set
```

**解决方案**: 确保 `clickhouse_datasource.json` 中包含 `"server": "host.docker.internal"`

### 问题2: 面板无数据
```
No data
```

**检查步骤**:
1. 确认 ClickHouse 服务运行中: `docker ps | grep clickhouse`
2. 验证数据存在:
   ```bash
   docker exec trading_clickhouse clickhouse-client --query \
     "SELECT count() FROM llm.llm_sessions WHERE trading_day = today()"
   ```
3. 检查数据源配置: Grafana → Configuration → Data sources → ClickHouse

### 问题3: 时间显示异常
如果时间显示为 `03:October:42` 格式，说明使用了旧版本配置。

**解决方案**: 使用本目录的 `ai_trader_dashboard.json` 重新导入（已修复）。

## 📝 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2025-10-29 | 初始版本 |
| 1.1 | 2025-10-29 | 修复时间格式显示问题 (`%M` → `%i`) |

---

**最后更新**: 2025-10-29
**维护者**: Trading Team
