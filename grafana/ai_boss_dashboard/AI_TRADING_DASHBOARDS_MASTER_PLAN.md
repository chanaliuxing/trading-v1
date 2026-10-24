# AI Trading Dashboards - Master Design Plan

**Created**: 2025-11-01 02:30 EST
**Purpose**: 完整的 AI 交易系统可视化方案 - 让 AI 大脑的行为完全透明

---

## 📋 Executive Summary

基于对以下内容的深入分析：
- `runs/ai_trader/` - AI 大脑决策日志（brain_calls.jsonl, brain_prompt_*.txt, *_plan.json）
- `logs/` - 系统运行日志（trader.log, gemini*.log, openai*.log, deepseek*.log）
- `llm_trader/` - LLM 交易逻辑代码
- `scripts/ai_trader/` - AI 交易脚本
- ClickHouse 数据库表（llm_decisions, llm_fills, llm_order_events, llm_sessions 等）

**设计 6 个核心 Dashboard**，从**交易员视角**和**管理员视角**全方位可视化 AI 交易系统。

---

## 🎯 Design Philosophy

### 交易员视角 (Trader Persona)
**目标**: 理解赚钱逻辑，优化策略，发现机会

**关注点**:
1. **今天赚了多少钱？** - P&L, Win Rate, Best/Worst Trades
2. **哪个策略最赚钱？** - 策略对比，A/B Test 结果
3. **AI 大脑在想什么？** - 决策过程，reasoning，news sentiment
4. **信号质量如何？** - 触发多少信号，被拦截多少，最终下单多少
5. **执行效果如何？** - Slippage, Fill Rate, Market Impact

### 管理员视角 (Admin/DevOps Persona)
**目标**: 确保系统稳定，优化资源，控制风险

**关注点**:
1. **系统健康状态？** - Uptime, API Status, Error Rate
2. **AI 模型表现？** - Latency, Token Usage, Cost
3. **风险控制如何？** - Position Size, Exposure, Drawdown
4. **资源使用情况？** - API Calls, Rate Limits, Costs
5. **异常告警？** - Failed Orders, Rejected Signals, System Errors

---

## 📊 Dashboard Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  AI Trading Dashboards Suite                 │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
    ┌───▼───┐          ┌───▼───┐          ┌───▼───┐
    │Trader │          │ Mixed │          │ Admin │
    │ View  │          │  View │          │ View  │
    └───┬───┘          └───┬───┘          └───┬───┘
        │                  │                   │
┌───────┼──────────────────┼───────────────────┼────────┐
│       │                  │                   │        │
│   ┌───▼────┐      ┌─────▼──────┐     ┌─────▼─────┐  │
│   │Dashboard│      │ Dashboard  │     │Dashboard  │  │
│   │   #1   │      │    #2-4    │     │  #5-6     │  │
│   └────────┘      └────────────┘     └───────────┘  │
│                                                       │
│   Dashboard 1: Trading Performance (Trader)          │
│   Dashboard 2: AI Brain Command Center (Mixed)       │
│   Dashboard 3: LLM Execution Deep Dive (Trader)      │
│   Dashboard 4: Strategy Deep Dive (Trader)           │
│   Dashboard 5: Risk & Compliance (Admin)             │
│   Dashboard 6: System Operations (Admin)             │
└───────────────────────────────────────────────────────┘
```

---

## 🎨 Dashboard Designs

### Dashboard 1: 💰 Trading Performance Dashboard
**Persona**: 交易员
**Goal**: 今天赚了多少钱？策略表现如何？

#### Data Sources
- `llm.llm_fills` - 成交记录
- `llm.llm_daily_pnl_view` - 每日盈亏
- `llm.llm_equity_timeseries` - 权益曲线
- `llm.ibkr_account_realtime` - 实时账户数据
- `llm.ibkr_positions_realtime` - 实时持仓
- `llm.llm_sessions` - 交易会话（session）

#### Row 1: 💵 Today's Performance (今日表现)
- **Panel 1.1**: Total P&L Today (今日盈亏)
  - Query: `SUM(realized_pnl + unrealized_pnl) FROM fills WHERE date = today()`
  - Display: Stat panel, Green/Red background
  - Threshold: Red < 0, Yellow 0-500, Green > 500

- **Panel 1.2**: Win Rate (胜率)
  - Query: `COUNT(CASE WHEN pnl > 0) / COUNT(*) * 100`
  - Display: Gauge, 0-100%

- **Panel 1.3**: Total Trades (成交次数)
  - Query: `COUNT(*) FROM llm_fills WHERE date = today()`
  - Display: Stat

- **Panel 1.4**: Best Trade (最佳交易)
  - Query: `MAX(realized_pnl) FROM fills WHERE date = today()`
  - Display: Stat with symbol

- **Panel 1.5**: Worst Trade (最差交易)
  - Query: `MIN(realized_pnl) FROM fills WHERE date = today()`
  - Display: Stat with symbol

- **Panel 1.6**: Average Trade Size (平均交易规模)
  - Query: `AVG(qty * price) FROM fills`
  - Display: Stat, currency

#### Row 2: 📈 Equity & Drawdown (权益与回撤)
- **Panel 2.1**: Equity Curve Today (今日权益曲线)
  - Query: Timeseries from `llm_equity_timeseries WHERE date = today()`
  - Display: Line chart (Equity, Cash, Position Value)

- **Panel 2.2**: Intraday Drawdown (日内回撤)
  - Query: `MIN((equity - max(equity)) / max(equity)) * 100`
  - Display: Gauge, Red if < -5%

- **Panel 2.3**: Rolling Sharpe (7-day)
  - Query: Calculate from daily returns
  - Display: Stat

#### Row 3: 📊 P&L Breakdown (盈亏分解)
- **Panel 3.1**: P&L by Session (按时段盈亏)
  - Query: `SUM(pnl) FROM fills GROUP BY session (morning/intraday/afterhours/overnight)`
  - Display: Bar chart

- **Panel 3.2**: P&L by Symbol (按股票盈亏)
  - Query: `SUM(pnl) FROM fills GROUP BY symbol`
  - Display: Bar chart, sorted by P&L

- **Panel 3.3**: P&L by Strategy (按策略盈亏)
  - Query: `SUM(pnl) FROM fills JOIN decisions ON decision_id GROUP BY strategy_id`
  - Display: Bar chart

#### Row 4: 📋 Trade History (交易历史)
- **Panel 4.1**: Today's Fills Table (今日成交明细)
  - Query: All fills today with symbol, qty, price, side, pnl, session
  - Display: Table with color-coded P&L

---

### Dashboard 2: 🧠 AI Brain Command Center
**Persona**: 混合（交易员+管理员）
**Goal**: AI 大脑在干什么？决策过程是什么？

#### Data Sources
- `runs/ai_trader/{date}/brain_calls.jsonl` - AI 大脑调用记录
- `runs/ai_trader/{date}/*_plan.json` - 执行计划
- `runs/ai_trader/{date}/brain_prompt_*.txt` - Prompt 历史
- `llm.llm_decisions` - LLM 决策记录
- `logs/trader.log` - 结构化日志

**注意**: 这个 Dashboard 需要将文件日志导入 ClickHouse！

#### Row 1: 🎯 AI Brain Status (AI 大脑状态)
- **Panel 1.1**: Current Session (当前会话)
  - Query: Latest session from brain_calls.jsonl
  - Display: Stat (morning/intraday/afterhours/overnight/learn)

- **Panel 1.2**: Active Providers (活跃提供商)
  - Query: Providers from current executed plan
  - Display: Stat list (Gemini, OpenAI, DeepSeek)

- **Panel 1.3**: Trading Symbols (交易标的)
  - Query: Symbols from current plan
  - Display: Tag cloud

- **Panel 1.4**: Safe Mode (安全模式)
  - Query: safe_mode flag from plan
  - Display: Boolean indicator

- **Panel 1.5**: AB Test Split (A/B 分配)
  - Query: AB ratios from plan
  - Display: Pie chart (Champion 70%, Challenger 30%)

#### Row 2: 🗓️ Session Timeline (会话时间线)
- **Panel 2.1**: Brain Decisions Timeline (大脑决策时间线)
  - Query: All brain_calls today with kind, provider, duration_ms
  - Display: Timeline/Gantt chart
  - Shows: propose_plan, execute_plan, reflection events

- **Panel 2.2**: Session Duration (会话时长)
  - Query: Duration of each session
  - Display: Bar chart

#### Row 3: 🧩 Decision Deep Dive (决策深入分析)
- **Panel 3.1**: Latest Brain Plan (最新大脑计划)
  - Query: Latest plan.json content
  - Display: JSON viewer with:
    - Providers (name, interval_sec, cycles, symbols, ab_group)
    - Flags (close_at, forbid_new_before_min)
    - Restrictions (safe_mode, no_new_positions, etc.)
    - Notes (reasoning)

- **Panel 3.2**: Provider Allocation by Session (提供商分配)
  - Query: Providers used in each session today
  - Display: Stacked bar chart
  - X-axis: Session (morning, intraday, afterhours, overnight)
  - Y-axis: # of symbols
  - Colors: Gemini, OpenAI, DeepSeek, etc.

#### Row 4: 💡 AI Reasoning & Notes (AI 推理与注释)
- **Panel 4.1**: Latest Reasoning (最新推理)
  - Query: `notes` field from latest brain_call
  - Display: Text panel

- **Panel 4.2**: Restrictions & Warnings (限制与警告)
  - Query: restrictions, safe_mode, dq_blacklist
  - Display: Alert list

- **Panel 4.3**: News Sentiment Impact (新闻情绪影响)
  - Query: need_news symbols and strength
  - Display: Table (Symbol, Sentiment, Strength 0-3, Forward Looking)

#### Row 5: 📊 Brain Performance Metrics (大脑表现指标)
- **Panel 5.1**: Brain Call Latency (大脑调用延迟)
  - Query: `duration_ms` from brain_calls
  - Display: Timeseries, p50/p95/p99

- **Panel 5.2**: Plan Changes (计划变更)
  - Query: Count of plan changes per session
  - Display: Counter

- **Panel 5.3**: Memory Utilization (记忆利用率)
  - Query: Check if top_providers, top_symbols from memory were used
  - Display: Percentage

---

### Dashboard 3: 🤖 LLM Execution Deep Dive
**Persona**: 交易员
**Goal**: 每个 LLM 到底干的怎么样？每个 Prompt 返回什么？

#### Data Sources
- `llm.llm_decisions` - LLM 决策记录
- `llm.llm_order_events` - 订单事件
- `logs/gemini_*.log`, `logs/openai_*.log`, `logs/deepseek_*.log` - LLM 日志
- `logs/trader.log` - 订单跳过原因

**注意**: 需要将 LLM provider logs 导入 ClickHouse！

#### Row 1: 🔍 LLM Provider Overview (LLM 提供商总览)
- **Panel 1.1**: Decisions by Provider (按提供商决策数)
  - Query: `COUNT(*) FROM llm_decisions GROUP BY provider (extract from decision_json)`
  - Display: Pie chart

- **Panel 1.2**: Average Latency by Provider (平均延迟)
  - Query: AVG(latency) from LLM logs
  - Display: Bar chart

- **Panel 1.3**: Token Usage by Provider (Token 使用量)
  - Query: SUM(prompt_tokens + completion_tokens) from LLM logs
  - Display: Bar chart

- **Panel 1.4**: Cost by Provider (成本)
  - Query: Calculate from token usage * pricing
  - Display: Stat, currency

#### Row 2: 📝 Decision Quality (决策质量)
- **Panel 2.1**: Decisions Table (决策表)
  - Query: Latest 50 decisions with:
    - ts_ny
    - symbol
    - provider (parsed from decision_json)
    - signal (BUY/SELL/HOLD)
    - confidence
    - reasoning (parsed from raw_text)
    - result (filled/skipped/rejected)
  - Display: Table with expandable reasoning

- **Panel 2.2**: Decision Distribution (决策分布)
  - Query: COUNT(*) GROUP BY signal (BUY/SELL/HOLD)
  - Display: Pie chart

#### Row 3: 🚫 Signal Filtering (信号过滤)
- **Panel 3.1**: Signal Funnel (信号漏斗)
  - Query:
    - Signals Generated (from llm_decisions)
    - Signals Passed Risk (not in order_skipped with reason=risk)
    - Signals Passed Market Filter (not skipped with reason=market_filter)
    - Signals Submitted (from llm_fills)
  - Display: Funnel chart

- **Panel 3.2**: Skip Reasons (跳过原因)
  - Query: `COUNT(*) FROM trader.log WHERE event_name = 'order_skipped' GROUP BY reason`
  - Display: Bar chart
  - Reasons: market_filter, risk_check, position_limit, low_confidence, etc.

- **Panel 3.3**: Skip Reason Details (跳过详情)
  - Query: reason_detail from order_skipped events
  - Display: Table (Reason, Detail, Count, Examples)

#### Row 4: 🎯 Execution Analysis (执行分析)
- **Panel 4.1**: Fill Rate by Provider (成交率)
  - Query: `COUNT(fills) / COUNT(decisions) * 100 GROUP BY provider`
  - Display: Bar chart

- **Panel 4.2**: Average Signal Confidence (平均信号置信度)
  - Query: AVG(confidence) from decisions GROUP BY provider
  - Display: Bar chart

- **Panel 4.3**: P&L by Provider (按提供商盈亏)
  - Query: SUM(pnl) from fills JOIN decisions GROUP BY provider
  - Display: Bar chart, color-coded

#### Row 5: 📜 Prompt Analysis (Prompt 分析)
- **Panel 5.1**: Prompt Templates Used (使用的 Prompt 模板)
  - Query: COUNT(*) GROUP BY execution_prompt_id
  - Display: Table

- **Panel 5.2**: Prompt Performance (Prompt 表现)
  - Query: Compare fill_rate, win_rate by prompt_id
  - Display: Table (Prompt ID, Fills, Win Rate, Avg P&L)

---

### Dashboard 4: 📈 Strategy Deep Dive
**Persona**: 交易员
**Goal**: 每个策略怎么跑的？触发了多少信号？Risk 怎么控制的？

#### Data Sources
- `llm.llm_fills` - 成交
- `llm.llm_decisions` - 决策
- `llm.llm_order_events` - 订单事件
- `logs/trader.log` - order_skipped events

#### Row 1: 📊 Strategy Performance (策略表现)
- **Panel 1.1**: P&L by Strategy (按策略盈亏)
  - Query: SUM(pnl) GROUP BY strategy_id
  - Display: Bar chart

- **Panel 1.2**: Strategy Win Rate (策略胜率)
  - Query: Win rate by strategy
  - Display: Table

- **Panel 1.3**: Strategy Sharpe Ratio (策略 Sharpe)
  - Query: Calculate from returns
  - Display: Bar chart

#### Row 2: 🎯 Signal Generation (信号生成)
- **Panel 2.1**: Signals Generated (生成的信号数)
  - Query: COUNT(*) FROM llm_decisions GROUP BY strategy_id
  - Display: Bar chart

- **Panel 2.2**: Signal Strength Distribution (信号强度分布)
  - Query: Histogram of signal confidence by strategy
  - Display: Heatmap

- **Panel 2.3**: Signal Frequency (信号频率)
  - Query: Signals per hour by strategy
  - Display: Timeseries

#### Row 3: 🛡️ Risk Control (风险控制)
- **Panel 3.1**: Signals Blocked by Risk (被风控拦截的信号)
  - Query: COUNT(*) WHERE event_name = 'order_skipped' AND reason = 'risk_check' GROUP BY strategy_id
  - Display: Bar chart

- **Panel 3.2**: Risk Block Reasons (风控拦截原因)
  - Query: reason_detail for risk_check blocks
  - Display: Table (Reason, Count, %)

- **Panel 3.3**: Position Size Distribution (仓位大小分布)
  - Query: Histogram of qty * price from fills
  - Display: Histogram

#### Row 4: ⏱️ Execution Timing (执行时机)
- **Panel 4.1**: Fills by Hour (按小时成交)
  - Query: COUNT(*) FROM fills GROUP BY hour(ts_ny)
  - Display: Heatmap (Hour x Strategy)

- **Panel 4.2**: Best Trading Hours (最佳交易时段)
  - Query: AVG(pnl) GROUP BY hour
  - Display: Bar chart

#### Row 5: 🔄 Strategy Evolution (策略演化)
- **Panel 5.1**: Strategy Parameters Over Time (策略参数变化)
  - Query: If parameters are logged, show changes
  - Display: Timeseries

- **Panel 5.2**: AB Test Results (A/B 测试结果)
  - Query: Compare champion vs challenger performance
  - Display: Comparison table

---

### Dashboard 5: ⚠️ Risk & Compliance Monitor
**Persona**: 管理员 + 风控
**Goal**: 风险控制是否有效？是否违规？

#### Data Sources
- `llm.ibkr_account_realtime` - 实时账户
- `llm.ibkr_positions_realtime` - 实时持仓
- `llm.llm_fills` - 成交
- `llm.llm_order_events` - 订单事件
- `logs/trader.log` - 风控事件

#### Row 1: 🎯 Risk Metrics (风险指标)
- **Panel 1.1**: Current Drawdown (当前回撤)
  - Query: (peak_equity - current_equity) / peak_equity
  - Display: Gauge, Alert if > 10%

- **Panel 1.2**: Max Drawdown Today (今日最大回撤)
  - Query: MAX(drawdown) WHERE date = today()
  - Display: Stat with alert

- **Panel 1.3**: VaR (Value at Risk)
  - Query: Calculate 95% VaR from returns
  - Display: Stat

- **Panel 1.4**: Position Concentration (仓位集中度)
  - Query: MAX(position_value) / total_equity
  - Display: Gauge, Alert if > 30%

#### Row 2: 📊 Exposure & Limits (敞口与限制)
- **Panel 2.1**: Gross Exposure (总敞口)
  - Query: SUM(ABS(position_value))
  - Display: Stat with % of equity

- **Panel 2.2**: Net Exposure (净敞口)
  - Query: SUM(position_value) (long - short)
  - Display: Stat

- **Panel 2.3**: Leverage (杠杆率)
  - Query: gross_exposure / equity
  - Display: Gauge

- **Panel 2.4**: Available Buying Power (可用购买力)
  - Query: buying_power from ibkr_account_realtime
  - Display: Stat

#### Row 3: 🚨 Risk Events (风险事件)
- **Panel 3.1**: Risk Blocks Today (今日风控拦截)
  - Query: COUNT(*) WHERE event_name = 'order_skipped' AND reason = 'risk_check'
  - Display: Stat

- **Panel 3.2**: Risk Block Timeline (风控拦截时间线)
  - Query: Timeseries of risk blocks
  - Display: Line chart

- **Panel 3.3**: Top Blocked Symbols (被拦截最多的股票)
  - Query: COUNT(*) GROUP BY symbol WHERE risk_check
  - Display: Table

#### Row 4: 📋 Compliance Checks (合规检查)
- **Panel 4.1**: Pattern Day Trader Status (PDT 状态)
  - Query: Check trades in last 5 days
  - Display: Boolean indicator

- **Panel 4.2**: Margin Requirements (保证金要求)
  - Query: init_margin_req, maint_margin_req from account
  - Display: Stats

- **Panel 4.3**: Restricted Symbols (限制股票)
  - Query: Symbols in dq_blacklist or per_symbol_override
  - Display: Tag cloud

---

### Dashboard 6: 🔧 System Operations
**Persona**: 管理员 / DevOps
**Goal**: 系统健康吗？资源用了多少？有没有异常？

#### Data Sources
- `logs/run_system.log` - 系统日志
- `logs/trader.log` - 交易日志
- `logs/*_error.log` - 错误日志
- `logs/system_health.json` - 系统健康状态
- LLM provider logs - API 调用日志

**注意**: 需要将日志导入 ClickHouse！

#### Row 1: 🟢 System Health (系统健康)
- **Panel 1.1**: System Uptime (系统运行时间)
  - Query: Time since last start
  - Display: Stat

- **Panel 1.2**: API Status (API 状态)
  - Query: Health check results (IBKR, Gemini, OpenAI, DeepSeek, ClickHouse)
  - Display: Status grid (Green/Red indicators)

- **Panel 1.3**: Error Rate (错误率)
  - Query: COUNT(*) WHERE level = 'error' / COUNT(*) * 100
  - Display: Gauge, Alert if > 5%

- **Panel 1.4**: Last Error Time (最后错误时间)
  - Query: MAX(timestamp) WHERE level = 'error'
  - Display: Stat

#### Row 2: 📈 API Usage & Performance (API 使用与性能)
- **Panel 2.1**: API Calls by Provider (按提供商 API 调用)
  - Query: COUNT(*) FROM LLM logs GROUP BY provider
  - Display: Bar chart

- **Panel 2.2**: API Latency (API 延迟)
  - Query: AVG(duration) from HTTP requests
  - Display: Timeseries (p50, p95, p99)

- **Panel 2.3**: API Errors (API 错误)
  - Query: COUNT(*) WHERE status_code >= 400
  - Display: Timeseries

- **Panel 2.4**: Rate Limit Status (速率限制状态)
  - Query: Check rate limit headers
  - Display: Gauge

#### Row 3: 💰 Cost Analysis (成本分析)
- **Panel 3.1**: Total Cost Today (今日总成本)
  - Query: SUM(token_cost) from all providers
  - Display: Stat

- **Panel 3.2**: Cost by Provider (按提供商成本)
  - Query: SUM(token_cost) GROUP BY provider
  - Display: Pie chart

- **Panel 3.3**: Cost Trend (成本趋势)
  - Query: Daily cost over last 30 days
  - Display: Timeseries

- **Panel 3.4**: Cost per Trade (每笔交易成本)
  - Query: total_cost / total_trades
  - Display: Stat

#### Row 4: 🚨 Alerts & Errors (告警与错误)
- **Panel 4.1**: Error Log (错误日志)
  - Query: Latest errors from all *_error.log files
  - Display: Table (Timestamp, Component, Error Message)

- **Panel 4.2**: Error Distribution (错误分布)
  - Query: COUNT(*) GROUP BY error_type
  - Display: Pie chart

- **Panel 4.3**: Warning Alerts (警告告警)
  - Query: COUNT(*) WHERE level = 'warning'
  - Display: Counter

#### Row 5: 📊 Resource Utilization (资源利用率)
- **Panel 5.1**: ClickHouse Storage (ClickHouse 存储)
  - Query: Database size from system.parts
  - Display: Stat

- **Panel 5.2**: Log File Sizes (日志文件大小)
  - Query: File sizes from filesystem
  - Display: Table

- **Panel 5.3**: Data Retention (数据保留)
  - Query: Oldest record in each table
  - Display: Table

---

## 🗄️ Data Pipeline Requirements

### Critical Gap: File Logs → ClickHouse
**当前问题**: `runs/ai_trader/` 和 `logs/` 的数据在文件中，无法直接用于 Grafana Dashboard

**解决方案**: 创建数据导入服务

#### New ClickHouse Tables Needed

```sql
-- AI Brain Calls
CREATE TABLE llm.ai_brain_calls (
    ts_ny DateTime('America/New_York'),
    date Date,
    kind String,  -- propose_plan, execute_plan, reflection
    session String,  -- morning, intraday, afterhours, overnight, learn
    provider String,  -- deepseek_nvidia, gemini, openai
    model String,
    duration_ms Int32,
    trading_universe_count Int32,
    plan_json String,  -- Full plan output
    input_keys Array(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts_ny);

-- AI Brain Plans
CREATE TABLE llm.ai_brain_plans (
    ts_ny DateTime('America/New_York'),
    date Date,
    session String,
    status String,  -- ok, error
    providers Array(String),
    symbols Array(String),
    safe_mode Bool,
    restrictions Array(String),
    ab_champion Float32,
    ab_challenger Float32,
    reasoning_profile String,
    notes String,
    plan_json String
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, session, ts_ny);

-- LLM Provider API Calls
CREATE TABLE llm.llm_api_calls (
    ts_ny DateTime('America/New_York'),
    date Date,
    provider String,
    model String,
    endpoint String,  -- chat/completions
    status_code Int32,
    latency_ms Int32,
    prompt_tokens Int32,
    completion_tokens Int32,
    total_tokens Int32,
    cost Float32,
    error_message String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts_ny, provider);

-- Order Skipped Events
CREATE TABLE llm.order_skipped_events (
    ts_ny DateTime('America/New_York'),
    date Date,
    strategy_id String,
    symbol String,
    signal Float32,
    confidence Float32,
    reason String,  -- market_filter, risk_check, position_limit
    reason_detail String,  -- low_volume, outside_hours, etc.
    event_json String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts_ny, symbol);

-- System Errors
CREATE TABLE llm.system_errors (
    ts_ny DateTime('America/New_York'),
    date Date,
    component String,  -- trader, brain, llm_provider
    level String,  -- error, warning
    error_type String,
    error_message String,
    stacktrace String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts_ny, component);
```

#### Data Ingestion Scripts

**Script 1**: `scripts/ingest_brain_logs.py`
```python
"""
Ingest runs/ai_trader/{date}/ files into ClickHouse
- brain_calls.jsonl → llm.ai_brain_calls
- *_plan.json → llm.ai_brain_plans
Run: Every 5 minutes via cron
"""
```

**Script 2**: `scripts/ingest_llm_logs.py`
```python
"""
Ingest logs/gemini_*.log, openai_*.log, deepseek_*.log
Parse HTTP Request logs → llm.llm_api_calls
Run: Every 5 minutes via cron
"""
```

**Script 3**: `scripts/ingest_trader_logs.py`
```python
"""
Ingest logs/trader.log (JSON lines)
- order_skipped events → llm.order_skipped_events
- error events → llm.system_errors
Run: Every 1 minute via cron
"""
```

---

## 🚀 Implementation Plan

### Phase 1: Data Foundation (Week 1)
**Goal**: 创建数据管道，导入所有日志到 ClickHouse

**Tasks**:
1. ✅ Create new ClickHouse tables (ai_brain_calls, ai_brain_plans, llm_api_calls, order_skipped_events, system_errors)
2. ✅ Write `ingest_brain_logs.py` - Parse brain_calls.jsonl and plans
3. ✅ Write `ingest_llm_logs.py` - Parse LLM provider logs
4. ✅ Write `ingest_trader_logs.py` - Parse trader.log JSON lines
5. ✅ Set up cron jobs for continuous ingestion
6. ✅ Verify data quality (run for 24h, check all tables have data)

**Deliverables**:
- 5 new ClickHouse tables with data
- 3 ingestion scripts running every 1-5 minutes
- Data validation report

### Phase 2: Core Dashboards (Week 2)
**Goal**: 实现 Dashboard 1, 2, 6（最重要的三个）

**Tasks**:
1. ✅ Dashboard 1: Trading Performance (trader.log, llm_fills, ibkr_account_realtime)
2. ✅ Dashboard 2: AI Brain Command Center (ai_brain_calls, ai_brain_plans)
3. ✅ Dashboard 6: System Operations (system_errors, llm_api_calls)

**Deliverables**:
- 3 working dashboards in Grafana
- Query verification scripts
- User testing with real traders

### Phase 3: Advanced Dashboards (Week 3)
**Goal**: 实现 Dashboard 3, 4, 5（深度分析）

**Tasks**:
1. ✅ Dashboard 3: LLM Execution Deep Dive
2. ✅ Dashboard 4: Strategy Deep Dive
3. ✅ Dashboard 5: Risk & Compliance Monitor

**Deliverables**:
- 3 advanced dashboards
- Integration with alerting system

### Phase 4: Polish & Alerts (Week 4)
**Goal**: 优化 UI，设置告警

**Tasks**:
1. ✅ UI/UX improvements (colors, layout, interactions)
2. ✅ Set up Grafana alerts (error rate, drawdown, API failures)
3. ✅ Create dashboard navigation/links
4. ✅ Write user documentation
5. ✅ Performance optimization (query caching, indexes)

**Deliverables**:
- Production-ready dashboard suite
- Alert rules configured
- User manual

---

## 📋 Success Metrics

### Trader Satisfaction
- ✅ Can answer "今天赚了多少钱？" in < 5 seconds
- ✅ Can drill down to specific trade in < 10 clicks
- ✅ Can understand AI reasoning without reading logs
- ✅ Can identify best/worst performing strategies instantly

### Admin/DevOps Satisfaction
- ✅ System health visible at a glance
- ✅ Errors detected within 1 minute
- ✅ Cost analysis available in real-time
- ✅ No need to SSH into server to check logs

### Technical Metrics
- ✅ Dashboard load time < 3 seconds
- ✅ Query execution time p95 < 1 second
- ✅ Data freshness < 5 minutes
- ✅ 99.9% uptime for dashboard service

---

## 🎨 UI/UX Principles

### Visual Design
1. **Color Coding**:
   - Green: Profit, Success, Healthy
   - Red: Loss, Error, Critical
   - Yellow/Orange: Warning, Caution
   - Blue: Neutral, Info
   - Gray: Disabled, N/A

2. **Typography**:
   - Large numbers for key metrics (P&L, Win Rate)
   - Monospace font for IDs, timestamps
   - Bold for important labels

3. **Layout**:
   - Most important metric = top-left
   - Time-series charts = wide panels
   - Tables = bottom rows
   - Drill-downs = expandable panels

### Interactivity
1. **Filters** (top of every dashboard):
   - Date range picker (today, yesterday, last 7 days, last 30 days)
   - Symbol filter (dropdown with autocomplete)
   - Session filter (morning/intraday/afterhours/overnight)
   - Provider filter (Gemini/OpenAI/DeepSeek)

2. **Drill-Downs**:
   - Click on symbol → Jump to Strategy Deep Dive for that symbol
   - Click on provider → Jump to LLM Execution Deep Dive for that provider
   - Click on error → Show full stacktrace

3. **Hover Info**:
   - All charts show value on hover
   - Tables show full reasoning text on hover

### Accessibility
- High contrast mode available
- Keyboard navigation support
- Screen reader friendly

---

## 📚 Documentation

### For Traders
- `TRADING_DASHBOARD_USER_GUIDE.md` - How to use Dashboard 1, 3, 4
- `AI_BRAIN_EXPLAINED.md` - Understanding Dashboard 2

### For Admins
- `SYSTEM_OPERATIONS_GUIDE.md` - How to use Dashboard 6
- `RISK_MONITOR_GUIDE.md` - How to use Dashboard 5
- `DATA_PIPELINE_MAINTENANCE.md` - Managing ingestion scripts

### For Developers
- `DASHBOARD_DEVELOPMENT.md` - Adding new panels
- `QUERY_OPTIMIZATION.md` - Performance tuning
- `ALERT_CONFIGURATION.md` - Setting up alerts

---

## 🔮 Future Enhancements (Phase 5+)

### Advanced Analytics
1. **Predictive Dashboards**:
   - Forecast tomorrow's P&L based on historical patterns
   - Predict best symbols for next session

2. **ML Model Performance**:
   - If using ML models, track their accuracy over time
   - Feature importance visualization

3. **Market Regime Detection**:
   - Classify market conditions (trending, ranging, volatile)
   - Show strategy performance by regime

### Integration
1. **Slack/Email Alerts**:
   - Push notifications for critical events
   - Daily P&L summary email

2. **Mobile Dashboards**:
   - Responsive design for mobile viewing
   - Native app integration

3. **Live Streaming**:
   - WebSocket for real-time updates (< 1 second latency)

---

**End of Master Plan**

**Next Steps**:
1. Review and approve this design
2. Start Phase 1: Data Foundation
3. Weekly progress reviews

**Estimated Total Time**: 4 weeks for full implementation
**Team Size**: 1-2 developers (can be done by Claude Code 😉)

---

**Document Version**: 1.0
**Last Updated**: 2025-11-01 02:30 EST
**Author**: Claude Code (with trading system expertise)
