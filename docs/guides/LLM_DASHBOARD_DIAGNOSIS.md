# LLM Trading Dashboard 诊断报告

**Dashboard**: http://localhost:3000/d/llm-trading-dashboard/llm-trading-complete-overview
**诊断时间**: 2025-11-01 23:45 EST
**状态**: 🔴 **所有数据显示 0 或过期 - 数据管道已停止**

---

## 🔍 根本原因

**所有 LLM 表今天没有任何新数据！**

| 表名 | 总行数 | 今日行数 | 最后更新时间 | 状态 |
|------|-------|---------|------------|------|
| `llm.llm_fills` | 7 | **0** | 2025-10-31 15:24:04 | 🔴 停止 32.4小时 |
| `llm.llm_decisions` | 464 | **0** | 2025-10-31 19:39:27 | 🔴 停止 28.1小时 |
| `llm.llm_equity_timeseries` | 613 | **0** | 2025-10-31 19:39:27 | 🔴 停止 28.1小时 |
| `llm.llm_positions_snapshot` | 10 | **0** | 2025-10-31 15:24:08 | 🔴 停止 32.4小时 |
| `llm.llm_daily_pnl_view` | 7 | **0** | 2025-10-31 | 🔴 停止 |

**结论**: 这不是 dashboard 配置问题，而是**数据源问题** - LLM trading 系统在昨天下午/晚上就已经停止运行！

---

## 📊 受影响的 Dashboard 面板

### Row 1: 💰 Financial Overview - **全部显示 0**

#### Panel 2: 💵 Total Equity (总权益)
- **Query**: `SELECT ... FROM llm.llm_equity_timeseries WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有数据，返回空结果或 0
- **影响**: 无法看到当前账户权益

#### Panel 3: 💵 Cash Available (可用现金)
- **Query**: `SELECT ... FROM llm.llm_equity_timeseries WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有数据
- **影响**: 无法看到可用现金

#### Panel 4: 📊 Today's PnL (今日盈亏)
- **Query**: `SELECT ... FROM llm.llm_daily_pnl_view WHERE trading_day = today()`
- **问题**: 今天没有数据
- **影响**: 显示 0 盈亏

#### Panel 5: 📦 Open Positions (持仓数)
- **Query**: `SELECT count(DISTINCT symbol) FROM llm.llm_positions_snapshot WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有快照
- **影响**: 显示 0 持仓

---

### Row 2: 📈 Trade Execution - **全部显示 0**

#### Panel 7: 📋 Today's Fills (今日成交)
- **Query**: `SELECT count() FROM llm.llm_fills WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有成交记录
- **影响**: 显示 0 笔成交

#### Panel 8: 📊 Fill Stats (成交统计)
- **Query**: Multiple queries on `llm.llm_fills WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有成交
- **影响**: 买入量、卖出量、成交价值全部显示 0

#### Panel 9: ⏱️ Fill Timeline (成交时间线)
- **Query**: `SELECT ts_ny, ... FROM llm.llm_fills WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有成交
- **影响**: 时间线为空

---

### Row 3: 🤖 AI Decision Analysis - **全部为空**

#### Panel 11: 🎯 Decision Actions (决策分布)
- **Query**: `SELECT action, count() FROM llm.llm_decisions WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有 AI 决策
- **影响**: 无法看到 BUY/SELL/HOLD 分布

#### Panel 12: 📊 Confidence Distribution (置信度分布)
- **Query**: `SELECT ... FROM llm.llm_decisions WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有决策
- **影响**: 置信度分布为空

#### Panel 13: 📝 Recent Decisions (最近决策)
- **Query**: `SELECT * FROM llm.llm_decisions WHERE toDate(ts_ny) = today() ORDER BY ts_ny DESC LIMIT 10`
- **问题**: 今天没有决策
- **影响**: 表格为空

---

### Row 4: 📦 Position Details - **可能有历史数据但不是今天的**

#### Panel 15: 📋 Current Positions (当前持仓)
- **Query**: Uses `anyLast()` on `llm.llm_positions_snapshot` - may show old data
- **问题**: 如果用 `anyLast()` 可能显示昨天的持仓，但不准确
- **影响**: 持仓数据过期（32小时前）

---

### Row 5: 📈 Equity Curve - **今天的曲线为空**

#### Panel 18: 📈 Equity Timeseries (权益走势)
- **Query**: `SELECT ts_ny, equity FROM llm.llm_equity_timeseries WHERE toDate(ts_ny) = today()`
- **问题**: 今天没有权益快照
- **影响**: 24小时图表中今天部分为空白

#### Panel 19: ⚠️ Drawdown (回撤)
- **Query**: Uses window function on `llm.llm_equity_timeseries`
- **问题**: 今天没有数据点，无法计算回撤
- **影响**: 回撤计算基于过期数据

---

## 🔧 解决方案

### ⚠️ 这不是 Dashboard 的问题，而是数据管道的问题！

**Dashboard 本身的 queries 是正确的** - 它们正确地查询 `WHERE toDate(ts_ny) = today()`。

**真正的问题**: LLM trading system 的数据写入进程在昨天就停止了。

---

## 🎯 立即行动项（按优先级）

### P0 - 紧急：诊断 LLM Trading System 为何停止

#### 1. 检查 LLM trading orchestrator/supervisor 进程状态

```bash
# 查找 LLM trading 相关进程
ps aux | grep -E "llm|trading|orchestr" | grep -v grep

# 检查最近的日志
tail -100 logs/llm_trading.log  # 或相应的日志文件
grep -i "error\|exception\|stopped" logs/llm_trading.log | tail -20

# 查看昨天 15:24 和 19:39 之间发生了什么（数据停止的时间点）
grep "2025-10-31 1[5-9]:" logs/llm_trading.log | tail -50
```

#### 2. 检查数据写入脚本/服务

可能的问题：
- 进程崩溃但没有自动重启
- 连接 IBKR/数据源失败
- ClickHouse 写入权限/连接问题
- Weekend mode 提前激活？
- Exception 没有被正确处理

#### 3. 查看是否有其他相关数据源也停止了

```sql
-- 检查其他可能的数据源
SELECT
    'ibkr_account_realtime' as source,
    max(ts_ny) as last_update,
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts_ny))) / 3600, 1) as hours_ago
FROM llm.ibkr_account_realtime
WHERE date >= today() - 1

UNION ALL

SELECT
    'ai_trader_dash_metrics' as source,
    max(ts) as last_update,
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts))) / 3600, 1) as hours_ago
FROM trading.ai_trader_dash_metrics
WHERE date >= today() - 1
```

---

### P1 - 临时解决方案：修改 Dashboard 显示昨天的数据

如果今天确实没有交易（周五市场关闭？），可以临时修改 dashboard 显示最近可用的数据：

#### 修改时间过滤器 - 从 `today()` 改为 `最近24小时`

**原始查询**:
```sql
SELECT ... FROM llm.llm_fills WHERE toDate(ts_ny) = today()
```

**临时修复**:
```sql
SELECT ... FROM llm.llm_fills WHERE ts_ny > now() - INTERVAL 24 HOUR
```

**优点**:
- 如果昨天有数据，至少能看到一些内容而不是全 0
- 更接近"滚动24小时"的语义

**缺点**:
- 不是"今日"数据
- 可能混合跨日数据

---

### P2 - 长期修复：添加数据新鲜度监控

#### 1. 在 Dashboard 顶部添加数据新鲜度指示器

```sql
-- 新增 Panel: Data Freshness Alert
SELECT
    'LLM Fills' as source,
    formatDateTime(max(ts_ny), '%Y-%m-%d %H:%M:%S') as last_update,
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts_ny))) / 60, 0) as minutes_ago,
    CASE
        WHEN (now() - max(ts_ny)) > INTERVAL 1 HOUR THEN '🔴 STALE'
        WHEN (now() - max(ts_ny)) > INTERVAL 30 MINUTE THEN '🟡 WARNING'
        ELSE '✅ FRESH'
    END as status
FROM llm.llm_fills

UNION ALL

SELECT
    'LLM Decisions' as source,
    formatDateTime(max(ts_ny), '%Y-%m-%d %H:%M:%S') as last_update,
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts_ny))) / 60, 0) as minutes_ago,
    CASE
        WHEN (now() - max(ts_ny)) > INTERVAL 1 HOUR THEN '🔴 STALE'
        WHEN (now() - max(ts_ny)) > INTERVAL 30 MINUTE THEN '🟡 WARNING'
        ELSE '✅ FRESH'
    END as status
FROM llm.llm_decisions
```

#### 2. 添加 Grafana Alert for LLM data staleness

```yaml
# grafana/provisioning/alerting/llm-data-alerts.yaml
- uid: llm-fills-staleness
  title: "🔴 P0: LLM Fills Data Stale > 1 Hour"
  condition: C
  data:
    - refId: A
      datasourceUid: ch
      model:
        rawSql: |
          SELECT round((now() - max(ts_ny)) / 60, 0) as minutes_ago
          FROM llm.llm_fills
  for: 5m
  annotations:
    summary: "LLM fills data is {{ $values.A.Value }} minutes old"
```

---

## 📋 快速诊断 Checklist

在修改 dashboard 之前，先确认系统状态：

- [ ] **Step 1**: 检查今天是否是交易日
  ```bash
  # 今天是周五 (2025-11-01) - 美股市场应该开盘
  # 如果市场关闭，数据停止是正常的
  ```

- [ ] **Step 2**: 检查 LLM trading 进程是否在运行
  ```bash
  ps aux | grep -E "llm.*trad" | grep -v grep
  ```

- [ ] **Step 3**: 检查日志最后的错误
  ```bash
  tail -50 logs/llm_trading.log  # 或相应路径
  ```

- [ ] **Step 4**: 检查 ClickHouse 连接是否正常
  ```bash
  docker exec trading_clickhouse clickhouse-client --query "SELECT 1"
  ```

- [ ] **Step 5**: 检查 IBKR 连接是否正常
  ```bash
  # 检查 ibkr_account_realtime 是否有今天的数据
  docker exec -i trading_clickhouse clickhouse-client --query "
  SELECT count() FROM llm.ibkr_account_realtime WHERE date = today()
  "
  ```

---

## 💡 推荐操作顺序

### 如果今天是交易日且市场开盘：

1. **立即调查**: 为什么 LLM trading system 停止写入数据（P0）
2. **临时修复**: Dashboard 查询改为 `last 24 hours` 而不是 `today()`（可选，如果需要快速看到一些数据）
3. **重启系统**: 如果发现进程崩溃，重启 LLM trading system
4. **验证恢复**: 检查新数据是否开始写入
5. **添加监控**: 添加数据新鲜度 alert（防止再次发生）

### 如果今天市场关闭（周末/节假日）：

1. **确认是正常行为**: LLM trading system 在非交易日不写入数据
2. **修改 Dashboard**: 查询改为 `last 24/48 hours` 或 `last trading day`
3. **添加说明**: 在 dashboard 顶部添加 "Showing last trading day data" 提示
4. **等待下一个交易日**: 周一验证数据恢复

---

## 🔗 相关问题

这个问题和之前发现的 **AI Boss Ideal V2 Dashboard** 问题是**同一个根本原因**：

- AI Boss dashboard: `ai_trader_dash_metrics` 停止在 14:49（今天）
- LLM Trading dashboard: 所有 `llm.*` 表停止在昨天 15:24-19:39

**共同点**: 整个 trading/AI system 的数据管道从昨天下午/晚上开始就已经停止运行！

**这意味着**:
- 🔴 **不仅仅是 dashboard 显示问题**
- 🔴 **整个系统可能已经停止交易 24+ 小时**
- 🔴 **需要紧急调查并重启系统**

---

## 📌 总结

| 问题 | 根本原因 | 解决方案 |
|------|---------|---------|
| **LLM Trading Dashboard 显示全 0** | 数据管道停止，今天没有新数据 | 调查并重启 LLM trading system |
| **所有 llm.* 表无今日数据** | 系统在昨天 15:24-19:39 之间停止 | 检查日志，找出停止原因 |
| **Dashboard queries 本身正确** | 不需要修改 queries | queries 是对的，数据源是问题 |

**优先级**:
1. 🔴 **P0**: 调查为什么 LLM trading system 停止运行
2. 🔴 **P0**: 重启系统（如果确认安全）
3. 🟡 **P1**: 添加数据新鲜度监控和 alerts
4. 🟢 **P2**: 考虑将 dashboard 查询从 `today()` 改为 `last 24h`（可选）

**当前状态**: 🔴 系统离线 32+ 小时，需要立即介入

---

**诊断完成时间**: 2025-11-01 23:45 EST
**下一步**: 调查系统日志 → 确认停止原因 → 重启 → 验证数据恢复
