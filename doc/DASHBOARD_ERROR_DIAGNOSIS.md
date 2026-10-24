# Dashboard Error Diagnosis - AI Boss Ideal V2

**Dashboard**: http://localhost:3000/d/ai-boss-ideal-v2/ai-boss-ideal-dashboard-v2
**Status**: ⚠️ **Many Panels Showing Errors**
**Date**: 2025-11-01 22:45 EST

---

## 🔍 **Root Cause Analysis**

### Problem 1: Empty Tables 🔴

**Evidence from ClickHouse**:
```sql
Table                    Size      Rows    Status
───────────────────────────────────────────────
ai_trader_dash_metrics   1.66 KB   252     ✅ HAS DATA (but old - last update 14:49)
equity_pnl_snapshots     0 B       0       ❌ EMPTY
fills_live               0 B       0       ❌ EMPTY
order_events             0 B       0       ❌ EMPTY
trading_signals          N/A       N/A     ❌ TABLE DOESN'T EXIST
```

### Problem 2: Data Pipeline Stopped

**Last Data Update**: 2025-11-01 14:49:01 (6.5 hours ago)

**All metrics stopped at 14:49**:
- `overlay_need_hedge`: 1
- `oos_mdd`: 0
- `guardian_events`: 0
- `hedging_events`: 0
- ... (all other metrics)

**This means**:
- 🔴 AI Trader orchestrator stopped updating metrics at 14:49
- 🔴 No new fills data (fills_live is empty)
- 🔴 No PnL snapshots being generated
- 🔴 Trading system effectively offline since 14:49

---

## 📊 **Panel-by-Panel Diagnosis**

### Row 1: 💰 顶级宏观概览

#### Panel: "💰 Total PnL"
**Status**: ❌ ERROR
**Query**: `SELECT ... FROM trading.equity_pnl_snapshots`
**Problem**: Table is EMPTY (0 rows)
**Fix**: Need to populate `equity_pnl_snapshots` or change query to use alternative data source

#### Panel: "📉 Max Drawdown"
**Status**: ⚠️ SHOWING OLD DATA
**Query**: `SELECT value FROM trading.ai_trader_dash_metrics WHERE name = 'oos_mdd'`
**Data**: Last update 14:49, value = 0
**Problem**: Data is 6.5 hours old
**Fix**: Restart orchestrator to resume metrics updates

#### Panel: "📈 Sharpe Ratio"
**Status**: ⚠️ SHOWING OLD DATA
**Query**: `SELECT value FROM trading.ai_trader_dash_metrics WHERE name = 'oos_sharpe'`
**Data**: Last update 14:49
**Problem**: Data is 6.5 hours old
**Fix**: Restart orchestrator

#### Panel: "🟢 System Status"
**Status**: ⚠️ SHOWING OLD DATA
**Query**: Multiple queries checking `dq_ok`, `tools_running`, etc.
**Problem**: All showing 6.5 hour old data
**Fix**: Restart orchestrator

#### Panel: "🛡️ Guardian Status"
**Status**: ⚠️ SHOWING OLD DATA
**Query**: `SELECT value FROM ... WHERE name = 'guardian_events'`
**Data**: 0 events (from 14:49)
**Fix**: Restart orchestrator

#### Panel: "💵 Account Equity (24h)"
**Status**: ❌ ERROR (or EMPTY)
**Query**: `SELECT ts, equity FROM trading.equity_pnl_snapshots WHERE ts > now() - INTERVAL 24 HOUR`
**Problem**: Table has 0 rows
**Fix**: Need data pipeline to populate this table

---

### Row 2: 🧠 AI 模型表现

#### Panel: "📊 Signal Distribution"
**Status**: ❌ ERROR
**Query**: `SELECT ... FROM trading.trading_signals`
**Problem**: Table `trading_signals` DOESN'T EXIST
**Fix**: Either:
1. Create table `trading_signals`
2. Change query to use existing table (e.g., `order_events`)

#### Panel: "🎯 Confidence Distribution"
**Status**: ❌ ERROR
**Query**: `SELECT ... FROM trading.trading_signals`
**Problem**: Table `trading_signals` DOESN'T EXIST
**Fix**: Same as above

---

## 🔧 **Fix Strategy**

### Immediate (Fix Dashboard Display)

#### Option 1: Use Alternative Data Sources ✅ RECOMMENDED

Replace broken queries with data that EXISTS:

**For PnL panels**: Use `llm.ibkr_account_realtime`
```sql
-- Instead of equity_pnl_snapshots, use IBKR account data
SELECT
    ts_ny as time,
    net_liquidation as equity
FROM llm.ibkr_account_realtime
WHERE ts_ny > now() - INTERVAL 24 HOUR
ORDER BY ts_ny
```

**For fills/trades**: Use `trading.fills_live` (but it's empty, need to fix data pipeline)
```sql
-- Check if any historical data exists
SELECT count() FROM trading.fills_live
-- Result: 0 (empty)
```

**For signals**: Remove panels or use `order_events`
```sql
SELECT * FROM trading.order_events LIMIT 1
-- Check if this table has data
```

#### Option 2: Add "No Data" Fallback

Modify queries to handle empty tables gracefully:
```sql
SELECT
    COALESCE(sum(realized_pnl), 0) as pnl,
    'No data available' as note
FROM trading.equity_pnl_snapshots
WHERE date = today()
```

---

### Medium-term (Fix Data Pipeline)

#### 1. Restart Orchestrator ⚠️ CRITICAL

**Current State**:
```bash
Process: weekend_supervisor.py - RUNNING
Process: auto_loop.py - RUNNING
Last metrics update: 14:49 (6.5 hours ago)
```

**Problem**: Processes are "running" but not actually doing anything

**Fix**:
```bash
# Check orchestrator logs
tail -100 logs/ai_trader.log

# Restart orchestrator
pkill -f "auto_loop.py"
pkill -f "weekend_supervisor.py"

# Start fresh
./venv/bin/python scripts/ai_trader/orchestrator.py
```

#### 2. Investigate Why Metrics Stopped at 14:49

**Possible causes**:
1. Weekend mode activated (today is Friday, market closed?)
2. Exception/crash in orchestrator (but process still running)
3. IBKR connection lost
4. Data quality gate failed

**Check logs**:
```bash
grep "14:49" logs/ai_trader.log
grep -i "error\|exception" logs/ai_trader.log | tail -20
```

#### 3. Populate Missing Tables

**Tables that exist but are empty**:
- `equity_pnl_snapshots` (0 rows)
- `fills_live` (0 rows)
- `order_events` (0 rows)

**Questions**:
- Should these tables have data?
- Is there an ETL process that should be running?
- Are they only populated during live trading?

---

### Long-term (Dashboard Improvement)

#### 1. Add Data Validation Panels

Add panels that show:
- Last data update time
- Missing tables
- Empty tables
- Data pipeline health

**Example**:
```sql
-- Data Freshness Indicator
SELECT
    name,
    formatDateTime(max(ts), '%Y-%m-%d %H:%M:%S') as last_update,
    round((now() - max(ts)) / 60, 1) as minutes_ago
FROM trading.ai_trader_dash_metrics
GROUP BY name
ORDER BY minutes_ago DESC
LIMIT 5
```

#### 2. Graceful Degradation

Modify all panels to:
- Show "No Data" message instead of error
- Display last known update time
- Highlight when data is stale

**Example transformation**:
```sql
-- Original (breaks if no data)
SELECT value FROM trading.ai_trader_dash_metrics WHERE name = 'oos_sharpe'

-- Improved (shows "No Data" if empty)
SELECT
    COALESCE(value, 0) as sharpe,
    CASE
        WHEN (now() - max(ts)) > INTERVAL 1 HOUR THEN '⚠️ STALE DATA'
        WHEN count() = 0 THEN '❌ NO DATA'
        ELSE '✅ OK'
    END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_sharpe' AND date = today()
```

#### 3. Create Separate "System Health" Dashboard

Move diagnostic panels to a dedicated dashboard:
- Data pipeline status
- Table row counts
- Last update times
- Missing data alerts

This keeps the "Boss Dashboard" clean and focused on trading metrics.

---

## 🎯 **Recommended Actions (Priority Order)**

### P0 - Critical (Do Now)

1. **Check if orchestrator is actually running**
   ```bash
   ps aux | grep orchestrator
   tail -50 logs/ai_trader.log
   ```

2. **Understand why metrics stopped at 14:49**
   - Is it weekend mode?
   - Did it crash?
   - Is market closed?

3. **Fix dashboard queries to use EXISTING data**
   - Replace `equity_pnl_snapshots` → `llm.ibkr_account_realtime`
   - Remove queries for non-existent `trading_signals` table
   - Add "No Data" fallbacks

---

### P1 - High (Do Today)

4. **Restart orchestrator** (if needed)
   - If it's truly stuck/crashed
   - After confirming it's safe (not in middle of trade)

5. **Update dashboard JSON with working queries**
   - Export current dashboard
   - Fix queries
   - Re-import

6. **Add data freshness indicators**
   - Show when data was last updated
   - Highlight stale data

---

### P2 - Medium (Do This Weekend)

7. **Investigate empty tables**
   - `equity_pnl_snapshots` - should this have data?
   - `fills_live` - should this have data?
   - `order_events` - should this have data?

8. **Document expected vs actual table usage**
   - Which tables are used in production?
   - Which are placeholders?
   - Which need to be populated?

9. **Create System Health dashboard**
   - Separate from Boss dashboard
   - Focus on infrastructure monitoring

---

## 📋 **Quick Fix SQL Queries**

### Replace equity_pnl_snapshots with IBKR data

**Original (broken)**:
```sql
SELECT ts, equity FROM trading.equity_pnl_snapshots WHERE ts > now() - INTERVAL 24 HOUR
```

**Fixed**:
```sql
SELECT
    ts_ny as time,
    net_liquidation as equity,
    unrealized_pnl
FROM llm.ibkr_account_realtime
WHERE ts_ny > now() - INTERVAL 24 HOUR
ORDER BY ts_ny
```

### Handle missing trading_signals table

**Original (broken)**:
```sql
SELECT signal, count() FROM trading.trading_signals WHERE ts > now() - INTERVAL 24 HOUR
```

**Option 1 - Remove panel** (if signals don't exist)

**Option 2 - Use order_events** (if that has signal data):
```sql
SELECT
    decision_type as signal,
    count() as frequency
FROM trading.order_events
WHERE ts > now() - INTERVAL 24 HOUR
GROUP BY decision_type
```

### Add data freshness check

**New panel query**:
```sql
SELECT
    'Last Update' as metric,
    formatDateTime(max(ts), '%Y-%m-%d %H:%M:%S') as value,
    CASE
        WHEN (now() - max(ts)) > INTERVAL 1 HOUR THEN 'STALE'
        ELSE 'OK'
    END as status
FROM trading.ai_trader_dash_metrics
WHERE date = today()
```

---

## 💡 **Summary**

**Root Cause**:
- 🔴 Data pipeline stopped at 14:49 (6.5 hours ago)
- 🔴 Many tables are empty or don't exist
- 🔴 Dashboard queries reference non-existent data

**Immediate Fix**:
- Modify dashboard queries to use existing data (`llm.ibkr_account_realtime` instead of `equity_pnl_snapshots`)
- Add "No Data" fallbacks
- Show data freshness indicators

**Long-term Fix**:
- Restart/debug orchestrator
- Populate missing tables (or remove unused tables)
- Create separate System Health dashboard
- Improve error handling in all panels

---

**Status**: 诊断完成 ✅
**Next Action**: 决定优先级 - 先修复 dashboard 显示还是先修复数据管道?
**Recommendation**: 两者并行 - 一边改 dashboard queries (15分钟),一边调查 orchestrator 为何停止 (需要时间)
