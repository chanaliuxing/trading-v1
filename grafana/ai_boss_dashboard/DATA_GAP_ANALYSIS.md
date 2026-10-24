# 🎯 AI Boss Dashboard - Data Gap Analysis

**Generated**: 2025-10-30 14:16 EDT
**Purpose**: Phase 1.2 - Map each planned panel to data availability
**Status**: ✅ Complete

---

## 📊 Panel-by-Panel Analysis

This document maps each panel from the original AI_BOSS_DASHBOARD_PLAN.md to actual data availability, providing clear go/no-go decisions for Phase 2 MVP implementation.

---

## Row 1: Executive Summary

### Panel 1.1: Total PnL (Stat Panel)

**Original Specification**:
```
Type: Stat Panel
Data Source: equity_pnl_snapshots
Query: SELECT latest equity - starting_equity AS total_pnl
Display: Large number with green/red background
Thresholds: Red < 0, Yellow 0-5000, Green > 5000
```

**Data Availability**: 🔴 BLOCKED
- **Table**: equity_pnl_snapshots = 0 rows
- **Reason**: Live trading not running

**Alternative for MVP**: 🟡 Use Backtest PnL Proxy
```sql
-- Show OOS trade count and Sharpe as performance proxy
SELECT
  value as trades
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_trades'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**MVP Decision**: ⏭️ DEFER to Phase 3
**MVP Replacement**: Show "OOS Sharpe Ratio" instead (Panel 1.1-alt below)

---

### Panel 1.1-alt: OOS Sharpe Ratio (MVP Replacement)

**New Specification**:
```
Type: Stat Panel
Data Source: ai_trader_dash_metrics
Metric: oos_sharpe
Display: Large number (e.g., "1.25")
Thresholds: Red < 0.5, Yellow 0.5-1.0, Green > 1.0
```

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

**Data Availability**: ✅ READY
- **Table**: ai_trader_dash_metrics ✅ 105 rows
- **Metric**: oos_sharpe available (avg=1.0, range 0-1.25)

**MVP Decision**: ✅ BUILD NOW (Priority: P0)

---

### Panel 1.2: Max Drawdown (Gauge)

**Original Specification**:
```
Type: Gauge
Data Source: equity_pnl_snapshots
Query: SELECT historical_peak, current_equity, (peak - current)/peak * 100 AS drawdown_pct
Display: Semi-circle gauge 0-10%
Thresholds: Green 0-2%, Yellow 2-5%, Red > 5%
```

**Data Availability**: 🔴 BLOCKED (equity_pnl_snapshots = 0 rows)

**Alternative for MVP**: 🟡 Use OOS MDD
```sql
SELECT
  value * 100 as drawdown_pct
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_mdd'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Data Availability**: ✅ READY
- **Table**: ai_trader_dash_metrics ✅ 105 rows
- **Metric**: oos_mdd available (avg=0.06, range 0-0.08)

**MVP Decision**: ✅ BUILD NOW with OOS data (Priority: P0)
**Caveat**: Label clearly as "OOS Max Drawdown" not "Live Drawdown"

---

### Panel 1.3: Sharpe Ratio (Stat)

**Original Specification**:
```
Type: Stat Panel
Data Source: equity_pnl_snapshots
Query: 30-day rolling returns → calculate Sharpe
Display: Single number (e.g., "1.8")
```

**Data Availability**: 🔴 BLOCKED (equity_pnl_snapshots = 0 rows)

**Alternative for MVP**: Same as Panel 1.1-alt (duplicate)

**MVP Decision**: ⏭️ SKIP (already covered by Panel 1.1-alt)

---

### Panel 1.4: System Status (Polystat)

**Original Specification**:
```
Type: Polystat Multi-Stat
Data Sources: Multiple services
Services: IBKR, Polygon, ClickHouse, Redis, Orchestrator, Brain
Display: Grid of colored squares (green/yellow/red)
```

**Data Availability**: 🟡 PARTIAL

**Available System Metrics**:
1. ✅ **Data Quality**: dq_ok, dq_failed_count, dq_passed_count
2. ✅ **Tools**: tools_running, tools_kill, tools_restart
3. ❌ **Service Health**: No direct health check data

**MVP Query**:
```sql
-- Data Quality Status
SELECT
  'Data Quality' as component,
  CASE WHEN value = 1 THEN 'OK' ELSE 'FAILED' END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'dq_ok'
  AND date = today()
ORDER BY ts DESC
LIMIT 1

UNION ALL

-- Tools Status
SELECT
  'Tools' as component,
  CASE WHEN value > 0 THEN 'RUNNING' ELSE 'STOPPED' END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'tools_running'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**MVP Decision**: ✅ BUILD NOW with 2 components (Priority: P1)
**Phase 3**: Add service health checks (IBKR, Polygon, etc.)

---

### Panel 1.5: Guardian Status (Text Panel)

**Original Specification**:
```
Type: Text Panel
Data Source: gate_snapshots
Query: SELECT state, last_trigger_reason
Display: "Active" (green) | "Tripped" (yellow) | "Halted" (red)
```

**Data Availability**: 🔴 BLOCKED (gate_snapshots = 0 rows)

**Alternative for MVP**: 🟡 Use guardian_events count
```sql
SELECT
  value as guardian_event_count,
  CASE
    WHEN value = 0 THEN 'Active - No Events'
    WHEN value < 5 THEN 'Warning - Events Detected'
    ELSE 'Alert - Multiple Events'
  END as status
FROM trading.ai_trader_dash_metrics
WHERE name = 'guardian_events'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

**Data Availability**: ✅ READY
- **Metric**: guardian_events (currently 0, which is healthy)

**MVP Decision**: ✅ BUILD NOW with event count (Priority: P1)

---

### Row 1 Summary

| Panel | Original Status | MVP Status | Priority | Notes |
|-------|----------------|------------|----------|-------|
| 1.1 Total PnL | 🔴 BLOCKED | ⏭️ REPLACED | - | Use 1.1-alt instead |
| 1.1-alt OOS Sharpe | - | ✅ BUILD | P0 | New panel |
| 1.2 Max Drawdown | 🔴 BLOCKED | ✅ BUILD | P0 | Use OOS MDD |
| 1.3 Sharpe Ratio | 🔴 BLOCKED | ⏭️ SKIP | - | Duplicate of 1.1-alt |
| 1.4 System Status | 🟡 PARTIAL | ✅ BUILD | P1 | DQ + Tools only |
| 1.5 Guardian | 🔴 BLOCKED | ✅ BUILD | P1 | Use event count |

**Row 1 MVP**: 4 panels buildable (1.1-alt, 1.2, 1.4, 1.5)

---

## Row 2: AI Brain Intelligence

### Panel 2.1: Signal Distribution (Histogram)

**Original Specification**:
```
Type: Histogram
Data Source: ai_trader_events or decision logs
Query: SELECT signal, COUNT(*) FROM decisions GROUP BY signal BUCKET 0.1
X-axis: Signal value (-1 to 1)
Y-axis: Frequency
```

**Data Availability**: 🔴 BLOCKED
- **Reason**: No signal field found in ai_trader_events.payload
- **Checked**: brain events contain strategic plans, not trading signals

**Investigation Needed**:
```sql
-- Check if any payloads contain 'signal' keyword
SELECT payload
FROM trading.ai_trader_events
WHERE payload LIKE '%signal%'
LIMIT 10
```

**MVP Decision**: ⏭️ DEFER to Phase 3 (need to locate signal data)

---

### Panel 2.2: Confidence Distribution (Histogram)

**Original Specification**:
```
Type: Histogram
Data Source: Same as 2.1
Query: SELECT confidence, COUNT(*) GROUP BY confidence BUCKET 0.1
```

**Data Availability**: 🔴 BLOCKED (same reason as 2.1)

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 2.3: High-Confidence Performance (Bar Chart)

**Original Specification**:
```
Type: Bar Chart
Data Source: Join decisions + outcomes
Query: Bin by confidence, calculate win rate per bin
```

**Data Availability**: 🔴 BLOCKED (need signal + confidence + outcome)

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 2.4: Feature Drift (Time Series)

**Original Specification**:
```
Type: Time Series
Data Source: Feature baseline vs current features
Query: SELECT feature_name, ABS(current - baseline) / baseline * 100
```

**Data Availability**: 🔴 BLOCKED
- **Reason**: No feature baseline table found

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 2.1-alt: Brain Call Frequency (MVP Replacement)

**New Specification**:
```
Type: Time Series
Data Source: ai_trader_events
Query: Count brain calls per hour, grouped by session
Display: Line chart with 3 series (overnight, premarket, intraday)
```

**Query**:
```sql
SELECT
  toStartOfHour(ts) as time,
  session,
  count() as brain_calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, session
ORDER BY time
```

**Data Availability**: ✅ READY
- **Table**: ai_trader_events ✅ 10,340 brain events

**MVP Decision**: ✅ BUILD NOW (Priority: P0)

---

### Panel 2.2-alt: Brain Calls (24h) - Stat

**New Specification**:
```
Type: Stat Panel
Data Source: ai_trader_events
Query: Count total brain calls in last 24h
Display: Large number (e.g., "10,340")
```

**Query**:
```sql
SELECT count() as brain_calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
```

**Data Availability**: ✅ READY

**MVP Decision**: ✅ BUILD NOW (Priority: P0)

---

### Panel 2.3-alt: Tool Executions (24h) - Stat

**New Specification**:
```
Type: Stat Panel
Data Source: ai_trader_events
Query: Count tool executions in last 24h
Display: Large number (e.g., "2,031")
```

**Query**:
```sql
SELECT count() as tool_executions
FROM trading.ai_trader_events
WHERE kind = 'tool'
  AND ts > now() - INTERVAL 24 HOUR
```

**Data Availability**: ✅ READY
- **Table**: ai_trader_events ✅ 2,031 tool events

**MVP Decision**: ✅ BUILD NOW (Priority: P0)

---

### Panel 2.4-alt: OOS Performance Table

**New Specification**:
```
Type: Table
Data Source: ai_trader_dash_metrics
Query: Latest values for oos_sharpe, oos_mdd, oos_trades
Display: 3 rows with metric name, value, status
```

**Query**:
```sql
SELECT
  name as metric,
  value,
  CASE
    WHEN name = 'oos_sharpe' AND value > 1.0 THEN 'Good'
    WHEN name = 'oos_mdd' AND value < 0.05 THEN 'Good'
    WHEN name = 'oos_trades' AND value > 20 THEN 'Good'
    ELSE 'Fair'
  END as status
FROM trading.ai_trader_dash_metrics
WHERE name IN ('oos_sharpe', 'oos_mdd', 'oos_trades')
  AND date = today()
ORDER BY ts DESC, name
LIMIT 3
```

**Data Availability**: ✅ READY

**MVP Decision**: ✅ BUILD NOW (Priority: P1)

---

### Row 2 Summary

| Panel | Original Status | MVP Status | Priority | Notes |
|-------|----------------|------------|----------|-------|
| 2.1 Signal Dist | 🔴 BLOCKED | ⏭️ REPLACED | - | No signal data |
| 2.2 Confidence Dist | 🔴 BLOCKED | ⏭️ REPLACED | - | No confidence data |
| 2.3 High-Conf Perf | 🔴 BLOCKED | ⏭️ REPLACED | - | Need signals + outcomes |
| 2.4 Feature Drift | 🔴 BLOCKED | ⏭️ REPLACED | - | No baseline data |
| 2.1-alt Brain Freq | - | ✅ BUILD | P0 | Time series |
| 2.2-alt Brain Count | - | ✅ BUILD | P0 | Stat panel |
| 2.3-alt Tool Count | - | ✅ BUILD | P0 | Stat panel |
| 2.4-alt OOS Table | - | ✅ BUILD | P1 | Performance table |

**Row 2 MVP**: 4 alternative panels buildable

---

## Row 3: Portfolio

### Panel 3.1: PnL by Strategy (Bar Chart)

**Original Specification**:
```
Type: Bar Chart
Data Source: portfolio_snapshots
Query: SELECT strategy_id, SUM(pnl) GROUP BY strategy_id
```

**Data Availability**: 🔴 BLOCKED
- **Table**: portfolio_snapshots = 0 rows

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 3.2: Current Positions (Table)

**Original Specification**:
```
Type: Table
Data Source: positions_snapshots
Columns: Symbol, Qty, Entry Price, Current Price, PnL, %
```

**Data Availability**: 🔴 BLOCKED
- **Table**: positions_snapshots = 0 rows

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 3.3: Exposure Breakdown (Pie Chart)

**Original Specification**:
```
Type: Pie Chart
Data Source: positions_snapshots
Query: Group positions by sector/asset class
```

**Data Availability**: 🔴 BLOCKED
- **Table**: positions_snapshots = 0 rows

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Row 3 Summary

| Panel | Original Status | MVP Status | Priority | Notes |
|-------|----------------|------------|----------|-------|
| 3.1 PnL by Strategy | 🔴 BLOCKED | ⏭️ DEFER | P3 | Need live trading |
| 3.2 Current Positions | 🔴 BLOCKED | ⏭️ DEFER | P3 | Need live trading |
| 3.3 Exposure | 🔴 BLOCKED | ⏭️ DEFER | P3 | Need live trading |

**Row 3 MVP**: ⏭️ DEFER entire row to Phase 3

---

## Row 4: Execution Quality

### Panel 4.1: Avg Slippage (Gauge)

**Original Specification**:
```
Type: Gauge
Data Source: fills_live JOIN order_events
Query: AVG((fill_price - order_price) / order_price * 10000) AS slippage_bps
```

**Data Availability**: 🔴 BLOCKED
- **Tables**: fills_live = 0 rows, order_events = 0 rows

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 4.2: Decision Funnel (Sankey/Bar)

**Original Specification**:
```
Type: Sankey Diagram
Data Source: exec_events
Stages: Generated → Risk-Checked → Submitted → Filled → Rejected
```

**Data Availability**: 🔴 BLOCKED
- **Tables**: exec_events = 0 rows, order_events = 0 rows

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Panel 4.3: Fill Rate (Stat)

**Original Specification**:
```
Type: Stat Panel
Data Source: fills_live
Query: (filled_orders / submitted_orders) * 100
```

**Data Availability**: 🔴 BLOCKED
- **Table**: fills_live = 0 rows

**MVP Decision**: ⏭️ DEFER to Phase 3

---

### Row 4 Summary

| Panel | Original Status | MVP Status | Priority | Notes |
|-------|----------------|------------|----------|-------|
| 4.1 Avg Slippage | 🔴 BLOCKED | ⏭️ DEFER | P3 | Need fills_live |
| 4.2 Decision Funnel | 🔴 BLOCKED | ⏭️ DEFER | P3 | Need exec_events |
| 4.3 Fill Rate | 🔴 BLOCKED | ⏭️ DEFER | P3 | Need fills_live |

**Row 4 MVP**: ⏭️ DEFER entire row to Phase 3

---

## 📊 Overall Dashboard Summary

### Original Plan (15 panels)
- Row 1: 5 panels
- Row 2: 4 panels
- Row 3: 3 panels
- Row 4: 3 panels

### MVP Implementation (8 panels)
- Row 1: 4 panels ✅ (1.1-alt, 1.2, 1.4, 1.5)
- Row 2: 4 panels ✅ (2.1-alt, 2.2-alt, 2.3-alt, 2.4-alt)
- Row 3: 0 panels ⏭️ (defer to Phase 3)
- Row 4: 0 panels ⏭️ (defer to Phase 3)

### Data Readiness Score

| Category | Total Panels | Buildable | Blocked | Deferred | Readiness |
|----------|--------------|-----------|---------|----------|-----------|
| Row 1 | 5 | 4 | 1 | 0 | 🟢 80% |
| Row 2 | 4 | 4 | 0 | 0 | 🟢 100% |
| Row 3 | 3 | 0 | 3 | 0 | 🔴 0% |
| Row 4 | 3 | 0 | 3 | 0 | 🔴 0% |
| **Total** | **15** | **8** | **7** | **0** | **🟡 53%** |

---

## 🎯 MVP Build Plan

### Priority P0 (Critical - Build First) - 5 panels

1. **OOS Sharpe Ratio** (Row 1.1-alt) - Stat
2. **OOS Max Drawdown** (Row 1.2) - Gauge
3. **Brain Call Frequency** (Row 2.1-alt) - Time Series
4. **Brain Calls (24h)** (Row 2.2-alt) - Stat
5. **Tool Executions (24h)** (Row 2.3-alt) - Stat

### Priority P1 (Important - Build Second) - 3 panels

6. **System Status** (Row 1.4) - Multi-Stat (DQ + Tools)
7. **Guardian Status** (Row 1.5) - Text Panel
8. **OOS Performance Table** (Row 2.4-alt) - Table

### Priority P3 (Phase 3 - When Live Trading Active) - 7 panels

9. **Total PnL** (Row 1.1) - Need equity_pnl_snapshots
10. **Signal Distribution** (Row 2.1) - Need signal data
11. **Confidence Distribution** (Row 2.2) - Need confidence data
12. **High-Confidence Performance** (Row 2.3) - Need signals + outcomes
13. **PnL by Strategy** (Row 3.1) - Need portfolio_snapshots
14. **Current Positions** (Row 3.2) - Need positions_snapshots
15. **Execution Quality panels** (Row 4.1-4.3) - Need order/fill data

---

## 📝 SQL Query Reference for MVP

### Row 1 Queries

#### OOS Sharpe Ratio (P0)
```sql
SELECT
  value as sharpe
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_sharpe'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

#### OOS Max Drawdown (P0)
```sql
SELECT
  value * 100 as drawdown_pct
FROM trading.ai_trader_dash_metrics
WHERE name = 'oos_mdd'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

#### System Status - Data Quality (P1)
```sql
SELECT
  'Data Quality' as component,
  CASE WHEN value = 1 THEN 'OK' ELSE 'FAILED' END as status,
  value
FROM trading.ai_trader_dash_metrics
WHERE name = 'dq_ok'
  AND date = today()
ORDER BY ts DESC
LIMIT 1
```

#### Guardian Status (P1)
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

### Row 2 Queries

#### Brain Call Frequency (P0)
```sql
SELECT
  toStartOfHour(ts) as time,
  session,
  count() as calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, session
ORDER BY time
```

#### Brain Calls 24h (P0)
```sql
SELECT count() as brain_calls
FROM trading.ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
```

#### Tool Executions 24h (P0)
```sql
SELECT count() as tool_executions
FROM trading.ai_trader_events
WHERE kind = 'tool'
  AND ts > now() - INTERVAL 24 HOUR
```

#### OOS Performance Table (P1)
```sql
SELECT
  name as metric,
  value,
  CASE
    WHEN name = 'oos_sharpe' AND value > 1.0 THEN '🟢 Good'
    WHEN name = 'oos_sharpe' AND value > 0.5 THEN '🟡 Fair'
    WHEN name = 'oos_mdd' AND value < 0.05 THEN '🟢 Good'
    WHEN name = 'oos_mdd' AND value < 0.10 THEN '🟡 Fair'
    WHEN name = 'oos_trades' AND value > 20 THEN '🟢 Good'
    ELSE '🟠 Poor'
  END as assessment
FROM trading.ai_trader_dash_metrics
WHERE name IN ('oos_sharpe', 'oos_mdd', 'oos_trades')
  AND date = today()
ORDER BY ts DESC, name
LIMIT 3
```

---

## 🔄 Phase 3 Requirements

When these tables have data, we can build the remaining panels:

### Required Tables
1. ✅ `equity_pnl_snapshots` - For live PnL tracking
2. ✅ `portfolio_snapshots` - For portfolio breakdown
3. ✅ `positions_snapshots` - For current positions
4. ✅ `order_events` - For order lifecycle
5. ✅ `fills_live` - For execution quality
6. ✅ `exec_events` - For decision funnel

### Required Fields
7. ✅ Trading signals (signal: -1 to 1)
8. ✅ Confidence scores (confidence: 0 to 1)
9. ✅ Trade outcomes (win/loss)
10. ✅ Feature baselines (for drift detection)

---

## 📋 Next Steps

1. ✅ Data availability checked
2. ✅ Gap analysis complete
3. ⏭️ **Next**: Update AI_BOSS_DASHBOARD_PLAN.md with MVP scope
4. ⏭️ **Next**: Build ai_boss_mvp.json with 8 panels
5. ⏭️ **Next**: Test in Grafana
6. ⏭️ **Future**: Phase 3 when live trading starts

---

**Document Status**: ✅ Complete
**Next Document**: Updated AI_BOSS_DASHBOARD_PLAN.md
**Last Updated**: 2025-10-30 14:16 EDT
