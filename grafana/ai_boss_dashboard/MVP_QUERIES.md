# AI Boss MVP Dashboard - SQL 查询参考

**用途**: Phase 2 实施时直接复制使用的 SQL 查询
**数据源**: ClickHouse (datasource UID: `ch`, database: `trading`)
**时区**: America/New_York

---

## Row 1: System Health (4 panels)

### Panel 1.1: OOS Sharpe Ratio (Stat Panel)

**Panel 配置**:
- Type: Stat
- Title: "OOS Sharpe Ratio"
- Description: "Out-of-sample Sharpe ratio from backtest"
- Unit: none (decimal)
- Decimals: 2

**Query**:
```sql
SELECT
  value as sharpe
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_sharpe'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Thresholds**:
- Red: < 0.5
- Yellow: 0.5 - 1.0
- Green: > 1.0

---

### Panel 1.2: OOS Max Drawdown (Gauge Panel)

**Panel 配置**:
- Type: Gauge
- Title: "OOS Max Drawdown"
- Description: "Out-of-sample maximum drawdown percentage"
- Unit: percent (0-100)
- Decimals: 1

**Query**:
```sql
SELECT
  value * 100 as drawdown_pct
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_mdd'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Thresholds**:
- Green: 0 - 5%
- Yellow: 5 - 10%
- Orange: 10 - 15%
- Red: > 15%

---

### Panel 1.3: System Status (Multi-Stat Panel)

**Panel 配置**:
- Type: Stat (Multi-query)
- Title: "System Status"
- Description: "Data Quality and Tools status"

**Query A - Data Quality**:
```sql
SELECT
  'Data Quality' as component,
  CASE
    WHEN value = 1 THEN '✅ OK'
    ELSE '❌ FAILED'
  END as status,
  value as metric_value
FROM trading.ai_trader_dash_metrics
WHERE name = 'dq_ok'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Query B - Tools Status**:
```sql
SELECT
  'Tools Running' as component,
  CASE
    WHEN value > 0 THEN '✅ ACTIVE'
    ELSE '⚪ STOPPED'
  END as status,
  value as metric_value
FROM trading.ai_trader_dash_metrics
WHERE name = 'tools_running'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Display**:
- Field: `status` (text)
- Color: Green for ✅, Red for ❌, Gray for ⚪

---

### Panel 1.4: Guardian Status (Stat Panel)

**Panel 配置**:
- Type: Stat
- Title: "Guardian Status"
- Description: "Risk guardian event count (0 = healthy)"
- Unit: none
- Decimals: 0

**Query**:
```sql
SELECT
  value as event_count,
  CASE
    WHEN value = 0 THEN '🟢 Active - No Events'
    WHEN value < 5 THEN '🟡 Warning - Events Detected'
    ELSE '🔴 Alert - Multiple Events'
  END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'guardian_events'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Thresholds**:
- Green: 0
- Yellow: 1-4
- Red: >= 5

---

## Row 2: Brain Activity (4 panels)

### Panel 2.1: Brain Calls (24h) - Stat

**Panel 配置**:
- Type: Stat
- Title: "Brain Calls (24h)"
- Description: "Total AI Brain calls in last 24 hours"
- Unit: short (number)
- Decimals: 0

**Query**:
```sql
SELECT count() as brain_calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
```

**Thresholds**:
- Info: Shows count, no color thresholds

---

### Panel 2.2: Brain Call Frequency (Time Series)

**Panel 配置**:
- Type: Time Series
- Title: "Brain Call Frequency"
- Description: "Brain calls per hour by session"
- Unit: short
- Decimals: 0

**Query**:
```sql
SELECT
  toStartOfHour(ts) as time,
  session,
  count() as calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > $__timeFrom()
  AND ts < $__timeTo()
GROUP BY time, session
ORDER BY time
```

**Legend**:
- Series: overnight, premarket, intraday (by session)
- Display: Line chart

---

### Panel 2.3: Tool Executions (24h) - Stat

**Panel 配置**:
- Type: Stat
- Title: "Tool Executions (24h)"
- Description: "Total tool executions in last 24 hours"
- Unit: short
- Decimals: 0

**Query**:
```sql
SELECT count() as tool_executions
FROM trading.ai_trader_events
WHERE kind = 'tool'
  AND ts > now() - INTERVAL 24 HOUR
```

**Thresholds**:
- Info: Shows count, no color thresholds

---

### Panel 2.4: OOS Performance Table

**Panel 配置**:
- Type: Table
- Title: "OOS Performance Metrics"
- Description: "Out-of-sample backtest performance"

**Query**:
```sql
WITH latest_metrics AS (
  SELECT
    name,
    value,
    ROW_NUMBER() OVER (PARTITION BY name ORDER BY ts DESC) as rn
  FROM trading.ai_trader_dash_metrics
  WHERE name IN ('oos_sharpe', 'oos_mdd', 'oos_trades')
    AND date = today()
)
SELECT
  CASE
    WHEN name = 'oos_sharpe' THEN 'Sharpe Ratio'
    WHEN name = 'oos_mdd' THEN 'Max Drawdown'
    WHEN name = 'oos_trades' THEN 'Trade Count'
  END as metric,
  CASE
    WHEN name = 'oos_sharpe' THEN round(value, 2)
    WHEN name = 'oos_mdd' THEN round(value * 100, 1)
    ELSE round(value, 0)
  END as value,
  CASE
    WHEN name = 'oos_sharpe' THEN ''
    WHEN name = 'oos_mdd' THEN '%'
    WHEN name = 'oos_trades' THEN 'trades'
  END as unit,
  CASE
    WHEN name = 'oos_sharpe' AND value > 1.0 THEN '🟢 Good'
    WHEN name = 'oos_sharpe' AND value > 0.5 THEN '🟡 Fair'
    WHEN name = 'oos_mdd' AND value < 0.05 THEN '🟢 Good'
    WHEN name = 'oos_mdd' AND value < 0.10 THEN '🟡 Fair'
    WHEN name = 'oos_trades' AND value > 20 THEN '🟢 Good'
    WHEN name = 'oos_trades' AND value > 10 THEN '🟡 Fair'
    ELSE '🟠 Poor'
  END as assessment
FROM latest_metrics
WHERE rn = 1
ORDER BY
  CASE
    WHEN name = 'oos_sharpe' THEN 1
    WHEN name = 'oos_mdd' THEN 2
    WHEN name = 'oos_trades' THEN 3
  END
```

**Columns**:
- Metric (text)
- Value (number)
- Unit (text)
- Assessment (text with emoji)

---

## 测试查询

### 验证数据可用性

```bash
# 检查 metrics 表
curl -s "http://localhost:8123/?database=trading&query=SELECT count() FROM ai_trader_dash_metrics FORMAT JSONCompact"

# 检查 events 表
curl -s "http://localhost:8123/?database=trading&query=SELECT count() FROM ai_trader_events FORMAT JSONCompact"

# 检查最新数据时间
curl -s "http://localhost:8123/?database=trading&query=SELECT max(ts) FROM ai_trader_dash_metrics FORMAT JSONCompact"
curl -s "http://localhost:8123/?database=trading&query=SELECT max(ts) FROM ai_trader_events FORMAT JSONCompact"
```

### 测试单个查询

```bash
# Test Panel 1.1 - OOS Sharpe
curl -s "http://localhost:8123/?database=trading" --data-binary "
SELECT value as sharpe
FROM ai_trader_dash_metrics
WHERE name = 'oos_sharpe' AND date = today()
ORDER BY ts DESC LIMIT 1
FORMAT JSONCompact
"

# Test Panel 2.1 - Brain Calls
curl -s "http://localhost:8123/?database=trading" --data-binary "
SELECT count() as brain_calls
FROM ai_trader_events
WHERE kind = 'brain' AND ts > now() - INTERVAL 24 HOUR
FORMAT JSONCompact
"

# Test Panel 2.2 - Brain Frequency
curl -s "http://localhost:8123/?database=trading" --data-binary "
SELECT
  toStartOfHour(ts) as time,
  session,
  count() as calls
FROM ai_trader_events
WHERE kind = 'brain' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, session
ORDER BY time
FORMAT JSONCompact
"
```

---

## Grafana Variables

### Dashboard Variables (Optional)

**Variable: time_range**
- Type: Custom
- Options: 1h, 6h, 12h, 24h, 7d
- Default: 24h
- Usage: Filter time range for panels

**Variable: session**
- Type: Query
- Query: `SELECT DISTINCT session FROM trading.ai_trader_events WHERE session != '' ORDER BY session`
- Multi-value: true
- Include All: true
- Usage: Filter Brain Call Frequency by session

---

## Dashboard Settings

### General Settings

```json
{
  "timezone": "America/New_York",
  "refresh": "30s",
  "time": {
    "from": "now-24h",
    "to": "now"
  },
  "title": "AI Boss - Trading Command Center (MVP)",
  "tags": ["ai-boss", "mvp", "monitoring"],
  "editable": true,
  "schemaVersion": 39
}
```

### Datasource

```json
{
  "datasource": {
    "type": "grafana-clickhouse-datasource",
    "uid": "ch"
  }
}
```

---

## Panel Layout (Grid Positions)

### Row 1: System Health

```
┌─────────────────────────────────────────────────────────┐
│ Row 1: System Health                                    │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│ OOS      │ OOS Max  │ System   │ Guardian │             │
│ Sharpe   │ Drawdown │ Status   │ Status   │             │
│ Ratio    │          │          │          │             │
│ (Stat)   │ (Gauge)  │ (Multi)  │ (Stat)   │             │
│ 6 wide   │ 6 wide   │ 6 wide   │ 6 wide   │             │
│ 4 high   │ 4 high   │ 4 high   │ 4 high   │             │
└──────────┴──────────┴──────────┴──────────┴─────────────┘
```

**Grid Positions**:
- Panel 1.1: x=0, y=1, w=6, h=4
- Panel 1.2: x=6, y=1, w=6, h=4
- Panel 1.3: x=12, y=1, w=6, h=4
- Panel 1.4: x=18, y=1, w=6, h=4

### Row 2: Brain Activity

```
┌─────────────────────────────────────────────────────────┐
│ Row 2: Brain Activity                                   │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│ Brain    │ Tool     │ Brain Call Frequency │ OOS        │
│ Calls    │ Exec     │ (Time Series)        │ Performance│
│ (24h)    │ (24h)    │                      │ Table      │
│ (Stat)   │ (Stat)   │ 12 wide, 6 high      │ (Table)    │
│ 3 wide   │ 3 wide   │                      │ 6w, 6h     │
│ 6 high   │ 6 high   │                      │            │
└──────────┴──────────┴──────────────────────┴────────────┘
```

**Grid Positions**:
- Panel 2.1: x=0, y=6, w=3, h=6
- Panel 2.2: x=3, y=6, w=3, h=6
- Panel 2.3: x=6, y=6, w=12, h=6
- Panel 2.4: x=18, y=6, w=6, h=6

---

## 常见问题

### Q1: 数据为空怎么办?

**检查**:
1. ClickHouse 服务是否运行: `docker ps | grep clickhouse`
2. 表是否有数据: `SELECT count() FROM trading.ai_trader_dash_metrics`
3. 日期是否正确: `SELECT distinct date FROM trading.ai_trader_dash_metrics`

**解决**:
- 如果表为空,等待 AI Trader 运行并写入数据
- 如果日期不是 today(),调整查询条件

### Q2: 时间显示错误?

**检查**:
- Dashboard timezone 设置: "America/New_York"
- ClickHouse timezone: `SELECT timezone()`
- Panel time format: 使用 `toString(ts)` 而不是 `formatDateTime()`

### Q3: Grafana 报错 "Plugin not registered"?

**解决**:
```bash
docker exec trading_grafana grafana cli plugins install grafana-clickhouse-datasource
docker restart trading_grafana
```

### Q4: 查询超时?

**优化**:
- 添加 date 索引: `WHERE date = today()`
- 限制时间范围: `AND ts > now() - INTERVAL 24 HOUR`
- 添加 LIMIT 子句

---

**文档版本**: 1.0
**最后更新**: 2025-10-30 14:16 EDT
**用途**: Phase 2 Implementation Reference
**状态**: ✅ Ready for Use
