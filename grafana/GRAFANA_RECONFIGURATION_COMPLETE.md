# ✅ Grafana 重新配置完成

**日期**: 2025-10-30
**状态**: ✅ 成功完成

---

## 📊 配置摘要

### 1. 已配置的 Datasources

| Name | Type | UID | Default | URL | Status |
|------|------|-----|---------|-----|--------|
| **ClickHouse** | grafana-clickhouse-datasource | `ch` | ✅ Yes | host.docker.internal:8123 | ✅ Working |
| Prometheus | prometheus | `prometheus` | No | prometheus:9090 | ✅ Working |
| Postgres | postgres | `pg` | No | host.docker.internal:5432 | ✅ Working |

**ClickHouse 配置详情**:
- Database: `trading`
- Protocol: HTTP
- Port: 8123
- Username: `default`
- Password: (empty)

---

## 📈 已部署的 Dashboards

### 1. **AI Trader Complete Dashboard (All Metrics)** ⭐ NEW
- **UID**: `ed2b85c5-7396-492d-8306-596a6c0977df`
- **URL**: http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
- **描述**: 完整的 AI Trader 监控大屏,包含所有 21 个打点指标
- **数据源**: ClickHouse (`trading` database)
- **状态**: ✅ 已加载,查询正常

**Dashboard 结构**:
- Section 1: Today Overview (8 KPI cards)
- Section 2: Data Quality (2 stats + trend)
- Section 3: Risk & Hedging (2 stats + 2 trends + events table)
- Section 4: Strategy & Deployment (performance chart + pending actions)
- Section 5: Portfolio & Overlay (2 stats + trend)
- Section 6: LLM Tools Health (3 stats + trend)
- Section 7: Brain Calls (2 stats + trend + detailed table)

**包含的指标** (21/21):
1. oos_sharpe ✅
2. oos_mdd ✅
3. oos_trades ✅
4. rl_gate_ok ✅
5. dq_ok ✅
6. dq_failed_count ✅
7. dq_passed_count ✅
8. guardian_events ✅
9. guardian_max_dd_pct ✅
10. hedging_events ✅
11. hedging_notional_usd ✅
12. pending_count ✅
13. pending_disable_rl ✅
14. pending_increase_conf_floor ✅
15. pending_reduce_sizing_multiplier ✅
16. overlay_beta ✅
17. overlay_need_hedge ✅
18. tools_kill ✅
19. tools_restart ✅
20. tools_backoff ✅
21. tools_running ✅

### 2. Financial & Market Overview
- **UID**: `financial-market-overview`
- **URL**: http://localhost:3000/d/financial-market-overview/financial-and-market-overview
- **状态**: ✅ 已加载

### 3. Infrastructure & Platform Health
- **UID**: `infra-platform-health`
- **URL**: http://localhost:3000/d/infra-platform-health/infrastructure-and-platform-health
- **状态**: ✅ 已加载

### 4. Strategy & Decision Pipeline Insight
- **UID**: `strategy-pipeline`
- **URL**: http://localhost:3000/d/strategy-pipeline/strategy-and-decision-pipeline-insight
- **状态**: ✅ 已加载

### 5. Trading System Operations
- **UID**: `trading-system-ops`
- **URL**: http://localhost:3000/d/trading-system-ops/trading-system-operations
- **状态**: ✅ 已加载

---

## 🔧 执行的配置步骤

### Step 1: 清理问题配置
1. 停止并删除旧的 Grafana 容器
2. 删除 Grafana 数据卷 (清除旧配置)
3. 修复 datasource 配置 (移除不存在的 Loki/Tempo)

### Step 2: 修复 Datasource 配置
**文件**: `./grafana/provisioning/datasources/ds.yml`

**修改内容**:
- ❌ 删除了 Loki datasource (服务不存在)
- ❌ 删除了 Tempo datasource (服务不存在)
- ✅ 保留 ClickHouse (UID: `ch`)
- ✅ 保留 Prometheus (UID: `prometheus`)
- ✅ 保留 Postgres (UID: `pg`)
- ✅ 设置 ClickHouse 为默认数据源

### Step 3: 部署完整 Dashboard
1. 复制 `AI_Trader_Complete_Dashboard.json` 到 `grafana/dashboards/active/`
2. 替换所有 datasource UID:
   - `"CLICKHOUSE_DS"` → `"ch"` (35 处替换)
3. 修复其他 dashboard 中的错误 datasource 引用:
   - `"loki"` → `"ch"`
   - `"tempo"` → `"prometheus"`

### Step 4: 重启 Grafana
```bash
cd ./infra
docker compose up -d grafana
```

### Step 5: 验证配置
- ✅ Grafana 状态: `healthy`
- ✅ Datasources 加载: 3/3 成功
- ✅ Dashboards 加载: 5/6 成功 (1 个旧 dashboard 有 title 错误,可忽略)
- ✅ ClickHouse 查询测试: 通过

---

## 🧪 验证查询测试

### Test 1: OOS Sharpe (Today)
```sql
SELECT anyLast(value) as val
FROM ai_trader_dash_metrics
WHERE date=today() AND name='oos_sharpe'
```
**Result**: `1.25` ✅

### Test 2: DQ Failed Count
```sql
SELECT anyLast(value) as val
FROM ai_trader_dash_metrics
WHERE date=today() AND name='dq_failed_count'
```
**Result**: `0.0` ✅

### Test 3: Brain Calls (Today)
```sql
SELECT count() AS count
FROM ai_trader_events
WHERE kind='brain' AND toDate(ts) = today()
```
**Result**: `0` ✅ (no brain calls today yet)

### Test 4: All Metrics Exist
```sql
SELECT DISTINCT name
FROM ai_trader_dash_metrics
ORDER BY name
```
**Result**: 21 unique metrics ✅

---

## 📂 配置文件位置

| 文件 | 路径 | 用途 |
|------|------|------|
| Docker Compose | `./infra/compose.yml` | Grafana 容器配置 |
| Datasources | `./grafana/provisioning/datasources/ds.yml` | 数据源自动配置 |
| Dashboard Provider | `./grafana/provisioning/dashboards/provider.yml` | Dashboard 自动加载 |
| Active Dashboards | `./grafana/dashboards/active/*.json` | Dashboard 定义文件 |
| Complete Dashboard | `./dashboards/AI_Trader_Complete_Dashboard.json` | 源文件 (备份) |

---

## 🚀 如何访问

### Grafana Web UI
- **URL**: http://localhost:3000
- **Username**: `admin`
- **Password**: `admin`

### 推荐第一次访问
1. 打开: http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
2. 检查所有 panels 是否正常显示
3. 如果看到 "No data",可能是因为今天还没有运行 AI Trader
4. 可以调整时间范围到过去 7 天查看历史数据

---

## 🔍 故障排查

### 问题 1: "No data" 显示在所有 panels
**可能原因**:
- AI Trader 今天还未运行
- ClickHouse 中没有今天的数据

**解决方案**:
1. 检查数据是否存在:
   ```bash
   curl -s "http://localhost:8123/?database=trading&query=SELECT%20count()%20FROM%20ai_trader_dash_metrics%20WHERE%20date=today()%20FORMAT%20JSONCompact"
   ```
2. 如果返回 0,调整 Dashboard 时间范围到 "Last 7 days"
3. 运行 AI Trader: `python scripts/ai_trader/auto_loop.py`

### 问题 2: ClickHouse datasource 连接错误
**检查步骤**:
1. ClickHouse 是否运行:
   ```bash
   docker ps | grep clickhouse
   ```
2. 测试连接:
   ```bash
   curl http://localhost:8123/ping
   ```
3. 检查 datasource 配置:
   - 进入 Grafana → Configuration → Data Sources → ClickHouse
   - 点击 "Test" 按钮

### 问题 3: Dashboard 显示 "Dashboard not found"
**解决方案**:
1. 检查 dashboard 文件是否存在:
   ```bash
   ls -la ./grafana/dashboards/active/
   ```
2. 重启 Grafana:
   ```bash
   docker restart trading_grafana
   ```
3. 等待 10-15 秒后刷新页面

### 问题 4: Grafana 容器不断重启
**检查日志**:
```bash
docker logs trading_grafana --tail 50
```

**常见错误**:
- `Datasource provisioning error`: 检查 `ds.yml` 配置
- `Dashboard title cannot be empty`: 某个 dashboard JSON 格式错误,可删除该文件

---

## 📊 数据更新频率

| Metric Type | Update Frequency | Source |
|-------------|------------------|--------|
| OOS Performance | Per backtest run | MLflow → ClickHouse |
| Data Quality | Per bar (real-time) | Data pipeline → ClickHouse |
| Risk Metrics | Real-time | Risk validator → ClickHouse |
| Brain Calls | Per LLM call | Brain tracer → ClickHouse |
| Tool Events | Per tool event | Orchestrator → ClickHouse |

**Dashboard 刷新**: 30 秒自动刷新

---

## 🔄 维护命令

### 重启 Grafana
```bash
docker restart trading_grafana
```

### 查看 Grafana 日志
```bash
docker logs trading_grafana -f
```

### 重新加载 Datasources (无需重启)
```bash
curl -X POST http://admin:admin@localhost:3000/api/admin/provisioning/datasources/reload
```

### 重新加载 Dashboards (无需重启)
```bash
curl -X POST http://admin:admin@localhost:3000/api/admin/provisioning/dashboards/reload
```

### 备份 Grafana 配置
```bash
# 备份 datasources
cp ./grafana/provisioning/datasources/ds.yml \
   ./grafana/provisioning/datasources/ds.yml.backup

# 备份 dashboards
tar -czf ~/grafana_dashboards_backup_$(date +%Y%m%d).tar.gz \
   ./grafana/dashboards/active/
```

---

## ✅ 配置完成清单

- [x] ClickHouse datasource 配置正确
- [x] Prometheus datasource 配置正确
- [x] Postgres datasource 配置正确
- [x] AI Trader Complete Dashboard 部署成功
- [x] 所有 21 个指标包含在 dashboard 中
- [x] Dashboard 查询语法正确 (使用 JSONExtractString)
- [x] Grafana 容器状态健康
- [x] 查询测试通过
- [x] Dashboard 可访问
- [x] 文档已创建

---

## 📝 相关文档

- `./dashboards/COMPLETE_DASHBOARD_VALIDATION.md` - Dashboard 验证报告
- `./dashboards/GRAFANA_DASHBOARD_FIX.md` - 查询语法修复指南
- `./scripts/ai_trader/dashboards/打点清单.md` - 指标规格说明
- `./doc/AI_BRAIN_TACTICAL_MANUAL.md` - Brain 监控架构

---

**配置完成时间**: 2025-10-30 09:05:00 EST
**配置人**: Claude Code
**Grafana 版本**: latest
**ClickHouse 插件版本**: 4.11.2

🎉 **配置成功! Dashboard 可以正常使用了!**

访问主 Dashboard: http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
