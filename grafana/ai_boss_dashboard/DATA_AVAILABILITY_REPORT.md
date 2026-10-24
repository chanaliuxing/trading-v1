# 📊 AI Boss Dashboard - Data Availability Report

**Generated**: 2025-10-30 14:16 EDT
**Purpose**: Phase 1.1 - Assess data availability for AI Boss Dashboard implementation
**Status**: ✅ Complete

---

## 📋 Executive Summary

### Available Data (Can Use Now)
- ✅ **ai_trader_dash_metrics**: 105 rows, 21 metrics, all from 打点清单.md
- ✅ **ai_trader_events**: 12,371 events (10,340 brain + 2,031 tool)
- ✅ **bars_5m_current**: 259,301 rows (market data)
- ✅ **market_index_snapshots**: 10 rows (SPY, QQQ indices)

### Missing Data (Need Live Trading)
- ⚪ **equity_pnl_snapshots**: 0 rows (account equity tracking)
- ⚪ **portfolio_snapshots**: 0 rows (portfolio composition)
- ⚪ **positions_snapshots**: 0 rows (current positions)
- ⚪ **order_events**: 0 rows (order lifecycle)
- ⚪ **fills_live**: 0 rows (execution fills)
- ⚪ **exec_events**: 0 rows (execution quality)
- ⚪ **gate_snapshots**: 0 rows (risk gate states)

### Impact Assessment
- **Row 1 (Executive Summary)**: 🔴 60% blocked - need PnL, drawdown, positions data
- **Row 2 (AI Brain Intelligence)**: 🟢 100% buildable - have all metrics and events
- **Row 3 (Portfolio)**: 🔴 80% blocked - need positions, PnL by strategy
- **Row 4 (Execution Quality)**: 🔴 100% blocked - need order/fill data

**Overall MVP Readiness**: 🟡 40% - Can build partial dashboard with AI metrics focus

---

## 📊 Detailed Table Analysis

### 1. ai_trader_dash_metrics ✅

**Status**: READY TO USE
**Row Count**: 105 rows
**Date Range**: 2025-10-30 (today only)
**Update Frequency**: Per AI Trader run

#### Available Metrics (21 total)

##### Performance Metrics (4)
| Metric | Records | Avg Value | Min | Max | Description |
|--------|---------|-----------|-----|-----|-------------|
| `oos_sharpe` | 5 | 1.00 | 0.00 | 1.25 | Out-of-sample Sharpe ratio |
| `oos_mdd` | 5 | 0.06 | 0.00 | 0.08 | Out-of-sample max drawdown |
| `oos_trades` | 5 | 36 | 0 | 45 | Out-of-sample trade count |
| `rl_gate_ok` | 5 | 0 | 0 | 0 | RL gate pass/fail |

##### Pending Actions (4)
| Metric | Records | Avg Value | Description |
|--------|---------|-----------|-------------|
| `pending_count` | 5 | 0 | Total pending actions |
| `pending_disable_rl` | 5 | 0 | Pending RL disable |
| `pending_increase_conf_floor` | 5 | 0 | Pending confidence floor increase |
| `pending_reduce_sizing_multiplier` | 5 | 0 | Pending sizing reduction |

##### Risk Management (4)
| Metric | Records | Avg Value | Description |
|--------|---------|-----------|-------------|
| `guardian_events` | 5 | 0 | Guardian intervention count |
| `guardian_max_dd_pct` | 5 | 0 | Guardian drawdown threshold |
| `hedging_events` | 5 | 0 | Hedging action count |
| `hedging_notional_usd` | 5 | 0 | Hedging notional value |

##### Data Quality (3)
| Metric | Records | Avg Value | Description |
|--------|---------|-----------|-------------|
| `dq_ok` | 5 | 1 | Data quality pass/fail |
| `dq_failed_count` | 5 | 0 | Failed DQ checks |
| `dq_passed_count` | 5 | 0 | Passed DQ checks |

##### Tools Management (4)
| Metric | Records | Avg Value | Description |
|--------|---------|-----------|-------------|
| `tools_kill` | 5 | 0 | Tool kill commands |
| `tools_restart` | 5 | 0 | Tool restart commands |
| `tools_backoff` | 5 | 0 | Tool backoff commands |
| `tools_running` | 5 | 0 | Currently running tools |

##### Overlay Strategy (2)
| Metric | Records | Avg Value | Description |
|--------|---------|-----------|-------------|
| `overlay_beta` | 5 | 0 | Portfolio beta |
| `overlay_need_hedge` | 5 | 0 | Hedge needed flag |

#### Schema
```sql
CREATE TABLE trading.ai_trader_dash_metrics (
    date Date,
    ts DateTime,
    run_name String,
    session LowCardinality(String),
    name LowCardinality(String),  -- metric name
    value Float64,                 -- metric value
    tags String                    -- JSON metadata
)
```

#### Usage Recommendations
✅ **Can Build Now**:
- All 21 metrics from 打点清单.md are available
- Use for Row 2 panels (AI Brain Intelligence)
- Time series charts showing metric evolution
- Stat panels for current values with thresholds

⚠️ **Limitations**:
- Only 1 day of data (need more history for trends)
- Some metrics are all zeros (not yet activated)
- No confidence/signal distribution data (would need different source)

---

### 2. ai_trader_events ✅

**Status**: READY TO USE
**Row Count**: 12,371 events
**Date Range**: 2025-10-29 04:42:50 to 2025-10-30 12:19:02 (~32 hours)
**Event Types**: brain (10,340) + tool (2,031)

#### Event Breakdown
| Kind | Count | Earliest | Latest | Description |
|------|-------|----------|--------|-------------|
| `brain` | 10,340 | 2025-10-29 04:42:50 | 2025-10-30 12:19:02 | AI Brain decisions |
| `tool` | 2,031 | 2025-10-29 09:31:46 | 2025-10-30 11:32:48 | Tool executions |

#### Schema
```sql
CREATE TABLE trading.ai_trader_events (
    date Date,
    ts DateTime,
    run_name String,
    session LowCardinality(String),  -- overnight/premarket/intraday
    kind LowCardinality(String),     -- brain/tool
    symbol String,
    payload String                    -- JSON event data
)
```

#### Sample Brain Event Payload
```json
{
  "ts": "2025-10-30T00:02:35.713352",
  "kind": "propose_plan",
  "session": "overnight",
  "provider": "deepseek_nvidia",
  "model": "deepseek-ai/deepseek-r1",
  "input_keys": ["health", "memory"],
  "output": {
    "session": "overnight",
    "providers": [
      {"name": "deepseek_nvidia", "interval_sec": 600, "cycles": 3},
      {"name": "gemini", "interval_sec": 300, "cycles": 6}
    ],
    "llm_trader_flags": {
      "close_at": "03:45",
      "forbid_new_before_min": 15
    },
    "ab": {"champion": 0.7, "challenger": 0.3},
    "notes": "System health warning..."
  }
}
```

#### Usage Recommendations
✅ **Can Build Now**:
- **Brain Call Frequency**: count() per hour/session
- **Brain Provider Distribution**: extract provider from payload
- **Brain Decision Timeline**: time series of calls
- **Tool Usage Patterns**: tool event frequency

⚠️ **Limitations**:
- No trading signal values (signal: -1 to 1) in payload
- No confidence scores (confidence: 0 to 1) in payload
- Brain events are strategic decisions, not trade signals
- Cannot build signal/confidence distribution histograms as planned

**Alternative**: If trading signals exist, they might be in:
- `order_events` table (currently empty)
- `exec_events` table (currently empty)
- A different events table (need to investigate)

---

### 3. bars_5m_current ✅

**Status**: READY TO USE
**Row Count**: 259,301 rows
**Symbols**: 15+ active in last 24h
**Purpose**: Market price data for context

#### Schema
```sql
CREATE TABLE trading.bars_5m_current (
    symbol String,
    bar_sec Int32,
    ts_start DateTime,
    ts_end DateTime,
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    vwap Float64,
    volume Float64
)
```

#### Usage Recommendations
✅ **Can Use For**:
- Background price charts
- Market context panels
- Symbol selector dropdown
- Correlation with AI decisions

---

### 4. market_index_snapshots ✅

**Status**: READY TO USE
**Row Count**: 10 rows
**Symbols**: SPY, QQQ
**Purpose**: Market index tracking

#### Usage Recommendations
✅ **Can Use For**:
- Market backdrop indicators
- Beta calculation context
- Hedge decision context

---

### 5. equity_pnl_snapshots ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 1 panels

#### Required For
- ❌ Total PnL (Stat Panel)
- ❌ PnL Time Series
- ❌ Sharpe Ratio calculation (need returns)
- ❌ Max Drawdown calculation

#### Workaround Options
1. **Use Simulated Data**: Generate sample data for layout testing
2. **Use oos_sharpe/oos_mdd**: Show backtest metrics instead
3. **Wait for Live Trading**: Implement when data available

**Recommendation**: Use workaround #2 for MVP - show backtest performance metrics

---

### 6. portfolio_snapshots ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 3 panels

#### Required For
- ❌ Portfolio Composition
- ❌ PnL by Strategy
- ❌ Exposure by Sector/Asset

#### Workaround Options
1. **Mock Current Portfolio**: Show static sample positions
2. **Show "No Positions"**: Empty state with explanation
3. **Wait for Live Trading**

**Recommendation**: Use workaround #2 for MVP - show "Awaiting Live Trading" message

---

### 7. positions_snapshots ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 1 + Row 3 panels

#### Required For
- ❌ Current Positions Table
- ❌ Position Sizing Charts
- ❌ Exposure Calculation

**Recommendation**: Same as portfolio_snapshots

---

### 8. order_events ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 4 panels

#### Required For
- ❌ Decision Funnel (generated → submitted → filled)
- ❌ Order latency
- ❌ Order rejection rate

#### Potential Alternative
- Check if `ai_trader_events` contains order-related events
- Parse payload for order decision data

**Recommendation**: Investigate payload further, otherwise wait for live trading

---

### 9. fills_live ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 4 panels

#### Required For
- ❌ Fill Rate
- ❌ Slippage Analysis
- ❌ Execution Quality Metrics

**Recommendation**: Wait for live trading

---

### 10. exec_events ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 4 panels

#### Required For
- ❌ Execution Timeline
- ❌ Slippage Distribution
- ❌ Fill Quality

**Recommendation**: Wait for live trading

---

### 11. gate_snapshots ⚪

**Status**: EMPTY (0 rows)
**Reason**: Live trading not yet running
**Impact**: BLOCKS Row 1 "Guardian Status" panel

#### Required For
- ❌ Guardian State (Active/Tripped/Halted)
- ❌ Risk Gate Status

#### Workaround Options
- Use `guardian_events` metric from ai_trader_dash_metrics
- Show "No Guardian Events" if metric = 0

**Recommendation**: Use workaround for MVP

---

## 🎯 MVP Buildability Assessment

### Row 1: Executive Summary (5 panels)

| Panel | Status | Data Source | Notes |
|-------|--------|-------------|-------|
| Total PnL | 🔴 BLOCKED | equity_pnl_snapshots | Empty table |
| Max Drawdown | 🟡 PARTIAL | ai_trader_dash_metrics.oos_mdd | Backtest only, not live |
| Sharpe Ratio | 🟡 PARTIAL | ai_trader_dash_metrics.oos_sharpe | Backtest only, not live |
| System Status | 🟢 BUILDABLE | ai_trader_dash_metrics (DQ, tools) | Can show DQ + tools status |
| Guardian Status | 🟡 PARTIAL | ai_trader_dash_metrics.guardian_events | Can show event count |

**Row 1 Readiness**: 🟡 40% - Can build 2/5 panels fully, 3/5 partially

---

### Row 2: AI Brain Intelligence (4 panels)

| Panel | Status | Data Source | Notes |
|-------|--------|-------------|-------|
| Signal Distribution | 🔴 BLOCKED | (unknown) | No signal data found |
| Confidence Distribution | 🔴 BLOCKED | (unknown) | No confidence data found |
| High-Confidence Performance | 🔴 BLOCKED | (unknown) | Need signal + confidence + outcome |
| Feature Drift | 🔴 BLOCKED | (unknown) | No feature baseline data |

**Alternative Panels We CAN Build**:
| Panel | Status | Data Source | Description |
|-------|--------|-------------|-------------|
| Brain Call Frequency | 🟢 BUILDABLE | ai_trader_events | Calls per hour by session |
| Brain Provider Mix | 🟢 BUILDABLE | ai_trader_events.payload | deepseek vs gemini vs nvidia |
| Tool Execution Rate | 🟢 BUILDABLE | ai_trader_events | Tool events over time |
| OOS Performance Metrics | 🟢 BUILDABLE | ai_trader_dash_metrics | Sharpe, MDD, Trades |

**Row 2 Readiness**: 🟡 50% - Original plan blocked, but can build 4 alternative panels

---

### Row 3: Portfolio (3 panels)

| Panel | Status | Data Source | Notes |
|-------|--------|-------------|-------|
| PnL by Strategy | 🔴 BLOCKED | portfolio_snapshots | Empty table |
| Current Positions | 🔴 BLOCKED | positions_snapshots | Empty table |
| Exposure Breakdown | 🔴 BLOCKED | positions_snapshots | Empty table |

**Row 3 Readiness**: 🔴 0% - All panels blocked

---

### Row 4: Execution Quality (3 panels)

| Panel | Status | Data Source | Notes |
|-------|--------|-------------|-------|
| Avg Slippage | 🔴 BLOCKED | fills_live, order_events | Empty tables |
| Decision Funnel | 🔴 BLOCKED | order_events, exec_events | Empty tables |
| Fill Rate | 🔴 BLOCKED | fills_live | Empty table |

**Row 4 Readiness**: 🔴 0% - All panels blocked

---

## 📝 Recommendations

### Phase 2: Build MVP Dashboard NOW

**Focus**: AI Brain Monitoring + Backtest Performance

#### Buildable Panels (8 total)

**Row 1: System Health (4 panels)**
1. ✅ **OOS Sharpe Ratio** - Latest `oos_sharpe` from ai_trader_dash_metrics
2. ✅ **OOS Max Drawdown** - Latest `oos_mdd` (gauge: 0-10%)
3. ✅ **Data Quality Status** - `dq_ok`, `dq_failed_count`, `dq_passed_count`
4. ✅ **Guardian Status** - `guardian_events` count (stat: 0 = healthy)

**Row 2: AI Brain Activity (4 panels)**
5. ✅ **Brain Calls (24h)** - count(ai_trader_events where kind='brain')
6. ✅ **Brain Call Timeline** - Time series of brain calls by session
7. ✅ **Tool Executions (24h)** - count(ai_trader_events where kind='tool')
8. ✅ **OOS Trade Count** - Latest `oos_trades` from metrics

**Row 3: Risk & Pending Actions (optional)**
9. ✅ **Pending Actions** - Sum of all `pending_*` metrics
10. ✅ **Risk Events** - `hedging_events`, `guardian_events`

#### Defer to Phase 3 (When Live Trading Active)
- ❌ Row 3: Portfolio panels (need positions_snapshots)
- ❌ Row 4: Execution Quality (need order_events, fills_live)
- ❌ Signal/Confidence distributions (need trading signal data)
- ❌ Live PnL tracking (need equity_pnl_snapshots)

---

## 🔍 Data Gaps - Investigation Needed

### Missing: Trading Signal & Confidence Data

**Expected**: Each trading decision should have:
- `signal`: Float64 (-1 to 1, where -1 = strong sell, 1 = strong buy)
- `confidence`: Float64 (0 to 1, higher = more certain)

**Current Status**: Not found in available tables

**Possible Locations**:
1. In `ai_trader_events.payload` (need to parse JSON)
2. In a different table not yet checked
3. Not yet implemented in AI Trader

**Action Required**:
```bash
# Check if signals are in brain event payloads
SELECT payload
FROM trading.ai_trader_events
WHERE kind = 'brain'
AND payload LIKE '%signal%'
LIMIT 10
```

### Missing: Feature Drift Baseline

**Expected**: Baseline feature values to compare against current features

**Current Status**: No baseline table found

**Action Required**: Ask user where feature baselines are stored

---

## 📋 Next Steps

### Immediate (Phase 1.2)
1. ✅ Generate this DATA_AVAILABILITY_REPORT.md
2. ⏭️ Generate DATA_GAP_ANALYSIS.md (panel-by-panel mapping)
3. ⏭️ Update AI_BOSS_DASHBOARD_PLAN.md with findings

### Phase 2 (Build MVP)
4. Create ai_boss_mvp.json with 8 buildable panels
5. Test dashboard in Grafana
6. Document limitations clearly

### Phase 3 (Future - When Live Trading Active)
7. Implement Portfolio row (3 panels)
8. Implement Execution Quality row (3 panels)
9. Add live PnL tracking
10. Add signal/confidence analysis (if data becomes available)

---

**Report Status**: ✅ Complete
**Next Document**: DATA_GAP_ANALYSIS.md
**Last Updated**: 2025-10-30 14:16 EDT
