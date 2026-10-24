# AI Boss Dashboard - Placeholder Implementation Guide

**Purpose**: 指导如何实现 7 个占位符 panels 的数据打点
**Date**: 2025-10-30

---

## 📋 快速概览

### 当前状态
- ✅ **8 panels** 已实现 (使用真实数据)
- 🟡 **7 panels** 占位符 (等待数据打点)

### 占位符清单

| Row | Panel | 数据需求 | 优先级 |
|-----|-------|---------|--------|
| 1 | Total PnL | equity_pnl_snapshots | P0 |
| 2 | Signal Distribution | trading_signals | P1 |
| 2 | Confidence Distribution | trading_signals | P1 |
| 2 | High-Conf Performance | trading_signals + outcomes | P1 |
| 2 | Feature Drift | feature_baselines | P2 |
| 3 | PnL by Strategy | portfolio_snapshots | P0 |
| 3 | Current Positions | positions_snapshots | P0 |
| 3 | Exposure Breakdown | positions_snapshots | P0 |
| 4 | Avg Slippage | fills_live + order_events | P1 |
| 4 | Decision Funnel | exec_events | P1 |
| 4 | Fill Rate | order_events | P1 |

---

## 🎯 Priority P0 - 实盘交易必需 (4 panels)

### 1. Total PnL (Row 1.1)

**所需表**: `equity_pnl_snapshots`

**Schema**:
```sql
CREATE TABLE IF NOT EXISTS trading.equity_pnl_snapshots (
    date Date,
    ts DateTime,
    equity Float64,
    realized_pnl Float64,
    unrealized_pnl Float64,
    cash Float64
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts);
```

**打点位置**:
- 文件: `src/brokers/ibkr/account_monitor.py` (或类似)
- 时机: 每次账户更新时 (建议每5秒或每分钟)

**代码示例**:
```python
async def log_equity_snapshot(self):
    """Log equity snapshot to ClickHouse"""
    account_value = await self.broker.get_account_value()

    query = """
        INSERT INTO trading.equity_pnl_snapshots
        (date, ts, equity, realized_pnl, unrealized_pnl, cash)
        VALUES
    """

    values = (
        datetime.now().date(),
        datetime.now(),
        account_value['total_value'],
        account_value['realized_pnl'],
        account_value['unrealized_pnl'],
        account_value['cash']
    )

    await self.clickhouse.execute(query, [values])
```

**Dashboard Query**:
```sql
SELECT
    anyLast(equity) - anyFirst(equity) as total_pnl
FROM trading.equity_pnl_snapshots
WHERE date = today()
```

---

### 2-4. Portfolio Panels (Row 3.1-3.3)

**所需表**: `portfolio_snapshots`, `positions_snapshots`

**Schema**:
```sql
-- Portfolio snapshots
CREATE TABLE IF NOT EXISTS trading.portfolio_snapshots (
    date Date,
    ts DateTime,
    strategy_id String,
    total_value Float64,
    pnl Float64,
    num_positions UInt16
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts, strategy_id);

-- Position snapshots
CREATE TABLE IF NOT EXISTS trading.positions_snapshots (
    date Date,
    ts DateTime,
    symbol String,
    qty Float64,
    side Enum8('LONG' = 1, 'SHORT' = -1),
    entry_price Float64,
    current_price Float64,
    pnl Float64,
    unrealized_pnl Float64,
    strategy_id String
) ENGINE = ReplacingMergeTree()
ORDER BY (date, ts, symbol);
```

**打点位置**:
- 文件: `src/portfolio/portfolio_manager.py`
- 时机: 每次持仓变化 + 定期快照 (每分钟)

**代码示例**:
```python
async def log_portfolio_snapshot(self):
    """Log portfolio and positions to ClickHouse"""

    # Portfolio snapshot
    for strategy_id, portfolio in self.portfolios.items():
        await self.clickhouse.execute("""
            INSERT INTO trading.portfolio_snapshots
            (date, ts, strategy_id, total_value, pnl, num_positions)
            VALUES
        """, [(
            datetime.now().date(),
            datetime.now(),
            strategy_id,
            portfolio.total_value,
            portfolio.total_pnl,
            len(portfolio.positions)
        )])

    # Position snapshots
    positions = []
    for pos in self.get_all_positions():
        positions.append((
            datetime.now().date(),
            datetime.now(),
            pos.symbol,
            pos.qty,
            'LONG' if pos.qty > 0 else 'SHORT',
            pos.entry_price,
            pos.current_price,
            pos.realized_pnl,
            pos.unrealized_pnl,
            pos.strategy_id
        ))

    if positions:
        await self.clickhouse.execute("""
            INSERT INTO trading.positions_snapshots
            (date, ts, symbol, qty, side, entry_price, current_price,
             pnl, unrealized_pnl, strategy_id)
            VALUES
        """, positions)
```

**Dashboard Queries**:
```sql
-- Row 3.1: PnL by Strategy
SELECT
    strategy_id,
    sum(pnl) as total_pnl
FROM trading.portfolio_snapshots
WHERE date = today()
GROUP BY strategy_id
ORDER BY total_pnl DESC;

-- Row 3.2: Current Positions
SELECT
    symbol, qty, entry_price, current_price, pnl
FROM trading.positions_snapshots
WHERE date = today()
  AND ts = (SELECT max(ts) FROM trading.positions_snapshots WHERE date = today())
ORDER BY abs(pnl) DESC;

-- Row 3.3: Exposure Breakdown
SELECT
    CASE
        WHEN symbol IN ('SPY', 'QQQ', 'IWM') THEN 'Index'
        WHEN symbol IN ('AAPL', 'MSFT', 'GOOGL') THEN 'Tech'
        ELSE 'Other'
    END as sector,
    sum(abs(qty * current_price)) as exposure
FROM trading.positions_snapshots
WHERE date = today()
  AND ts = (SELECT max(ts) FROM trading.positions_snapshots WHERE date = today())
GROUP BY sector;
```

---

## 🎯 Priority P1 - 交易分析 (6 panels)

### 5-7. Signal/Confidence Panels (Row 2.1-2.3)

**所需表**: `trading_signals`

**Schema**:
```sql
CREATE TABLE IF NOT EXISTS trading.trading_signals (
    date Date,
    ts DateTime,
    strategy_id String,
    symbol String,
    signal Float64,           -- -1 to 1
    confidence Float64,       -- 0 to 1
    decision Enum8('BUY' = 1, 'SELL' = -1, 'HOLD' = 0),
    executed Boolean,
    outcome Nullable(Float64), -- PnL if trade executed
    ts_exit Nullable(DateTime)
) ENGINE = MergeTree()
ORDER BY (date, ts, strategy_id, symbol);
```

**打点位置**:
- 文件: `src/strategies/base_strategy.py` (on_features 方法)
- 时机: 每次策略生成信号时

**代码示例**:
```python
async def on_features(self, symbol: str, features: Dict[str, Any]):
    """Strategy decision with signal logging"""

    # Generate signal and confidence
    signal = self.calculate_signal(features)  # -1 to 1
    confidence = self.calculate_confidence(features)  # 0 to 1

    # Decide action
    decision = 'BUY' if signal > 0.3 else 'SELL' if signal < -0.3 else 'HOLD'

    # Log signal
    await self.log_signal(symbol, signal, confidence, decision)

    # Execute if confidence high enough
    if confidence > self.min_confidence:
        await self.submit_order(symbol, decision)

async def log_signal(self, symbol, signal, confidence, decision):
    """Log trading signal to ClickHouse"""
    await self.clickhouse.execute("""
        INSERT INTO trading.trading_signals
        (date, ts, strategy_id, symbol, signal, confidence, decision, executed)
        VALUES
    """, [(
        datetime.now().date(),
        datetime.now(),
        self.strategy_id,
        symbol,
        signal,
        confidence,
        decision,
        confidence > self.min_confidence
    )])
```

**Dashboard Queries**:
```sql
-- Row 2.1: Signal Distribution
SELECT
    round(signal, 1) as signal_bucket,
    count() as frequency
FROM trading.trading_signals
WHERE ts > now() - INTERVAL 24 HOUR
GROUP BY signal_bucket
ORDER BY signal_bucket;

-- Row 2.2: Confidence Distribution
SELECT
    round(confidence, 1) as conf_bucket,
    count() as frequency
FROM trading.trading_signals
WHERE ts > now() - INTERVAL 24 HOUR
GROUP BY conf_bucket
ORDER BY conf_bucket;

-- Row 2.3: High-Confidence Performance
SELECT
    floor(confidence * 10) / 10 as conf_range,
    count() as total_trades,
    countIf(outcome > 0) as wins,
    countIf(outcome > 0) / count() * 100 as win_rate
FROM trading.trading_signals
WHERE executed = 1
  AND outcome IS NOT NULL
  AND ts > now() - INTERVAL 7 DAY
GROUP BY conf_range
ORDER BY conf_range;
```

---

### 8-10. Execution Quality Panels (Row 4.1-4.3)

**所需表**: `order_events`, `fills_live`, `exec_events`

**Schema**:
```sql
-- Order events
CREATE TABLE IF NOT EXISTS trading.order_events (
    date Date,
    ts DateTime,
    order_id String,
    symbol String,
    side Enum8('BUY' = 1, 'SELL' = -1),
    qty Float64,
    order_type Enum8('MKT' = 1, 'LMT' = 2, 'STP' = 3),
    limit_price Nullable(Float64),
    status Enum8('SUBMITTED' = 1, 'FILLED' = 2, 'CANCELLED' = 3, 'REJECTED' = 4),
    strategy_id String
) ENGINE = MergeTree()
ORDER BY (date, ts, order_id);

-- Fill events
CREATE TABLE IF NOT EXISTS trading.fills_live (
    date Date,
    ts DateTime,
    order_id String,
    fill_id String,
    symbol String,
    fill_price Float64,
    fill_qty Float64,
    commission Float64
) ENGINE = MergeTree()
ORDER BY (date, ts, order_id, fill_id);

-- Execution pipeline events
CREATE TABLE IF NOT EXISTS trading.exec_events (
    date Date,
    ts DateTime,
    decision_id String,
    stage Enum8('GENERATED' = 1, 'RISK_CHECK' = 2, 'SUBMITTED' = 3, 'FILLED' = 4, 'REJECTED' = 5),
    symbol String,
    details String
) ENGINE = MergeTree()
ORDER BY (date, ts, decision_id);
```

**打点位置**:
- 文件: `src/execution/order_executor.py`, `src/execution/order_router.py`
- 时机: 每个订单生命周期阶段

**代码示例**:
```python
async def submit_order(self, order_request):
    """Submit order with full logging"""

    order_id = generate_order_id()

    # Log order submission
    await self.log_order_event(order_id, order_request, 'SUBMITTED')

    # Submit to broker
    try:
        broker_order = await self.broker.submit_order(order_request)

        # Log fills as they come
        async for fill in self.broker.watch_fills(order_id):
            await self.log_fill(order_id, fill)

        await self.log_order_event(order_id, order_request, 'FILLED')

    except OrderRejectedException as e:
        await self.log_order_event(order_id, order_request, 'REJECTED')

async def log_order_event(self, order_id, order, status):
    """Log order event"""
    await self.clickhouse.execute("""
        INSERT INTO trading.order_events
        (date, ts, order_id, symbol, side, qty, order_type,
         limit_price, status, strategy_id)
        VALUES
    """, [(
        datetime.now().date(),
        datetime.now(),
        order_id,
        order.symbol,
        order.side,
        order.qty,
        order.order_type,
        order.limit_price,
        status,
        order.strategy_id
    )])

async def log_fill(self, order_id, fill):
    """Log fill event"""
    await self.clickhouse.execute("""
        INSERT INTO trading.fills_live
        (date, ts, order_id, fill_id, symbol, fill_price, fill_qty, commission)
        VALUES
    """, [(
        datetime.now().date(),
        datetime.now(),
        order_id,
        fill.fill_id,
        fill.symbol,
        fill.price,
        fill.qty,
        fill.commission
    )])
```

**Dashboard Queries**:
```sql
-- Row 4.1: Avg Slippage
SELECT
    avg((f.fill_price - o.limit_price) / o.limit_price * 10000) as slippage_bps
FROM trading.fills_live f
JOIN trading.order_events o ON f.order_id = o.order_id
WHERE f.date = today()
  AND o.order_type = 'LMT';

-- Row 4.2: Decision Funnel
SELECT
    stage,
    count(DISTINCT decision_id) as decisions
FROM trading.exec_events
WHERE date = today()
GROUP BY stage
ORDER BY stage;

-- Row 4.3: Fill Rate
SELECT
    countIf(status = 'FILLED') / count() * 100 as fill_rate_pct
FROM trading.order_events
WHERE date = today();
```

---

## 🎯 Priority P2 - 高级监控 (1 panel)

### 11. Feature Drift (Row 2.4)

**所需表**: `feature_baselines`, `feature_snapshots`

**Schema**:
```sql
CREATE TABLE IF NOT EXISTS trading.feature_baselines (
    feature_name String,
    baseline_value Float64,
    baseline_std Float64,
    created_date Date
) ENGINE = ReplacingMergeTree()
ORDER BY (feature_name);

CREATE TABLE IF NOT EXISTS trading.feature_snapshots (
    date Date,
    ts DateTime,
    symbol String,
    feature_name String,
    value Float64
) ENGINE = MergeTree()
ORDER BY (date, ts, symbol, feature_name);
```

**打点位置**:
- 文件: `src/features/feature_pipeline.py`
- 时机: 特征计算时

**代码示例**:
```python
async def compute_features(self, symbol: str, bars: pd.DataFrame):
    """Compute features and log for drift monitoring"""

    features = {
        'rsi_14': self.calculate_rsi(bars, 14),
        'ma_cross': self.calculate_ma_cross(bars),
        'volume_ratio': self.calculate_volume_ratio(bars),
        # ... more features
    }

    # Log feature snapshot
    await self.log_features(symbol, features)

    return features

async def log_features(self, symbol, features):
    """Log feature snapshot"""
    rows = [
        (
            datetime.now().date(),
            datetime.now(),
            symbol,
            feature_name,
            value
        )
        for feature_name, value in features.items()
    ]

    await self.clickhouse.execute("""
        INSERT INTO trading.feature_snapshots
        (date, ts, symbol, feature_name, value)
        VALUES
    """, rows)
```

**Dashboard Query**:
```sql
SELECT
    f.feature_name,
    f.ts as time,
    abs(f.value - b.baseline_value) / b.baseline_std as drift_zscore
FROM trading.feature_snapshots f
JOIN trading.feature_baselines b ON f.feature_name = b.feature_name
WHERE f.date = today()
  AND f.feature_name IN ('rsi_14', 'ma_cross', 'volume_ratio')
ORDER BY f.ts;
```

---

## 📋 实施清单

### Phase 1: 立即实施 (P0 - 实盘交易必需)
- [ ] equity_pnl_snapshots 打点
- [ ] portfolio_snapshots 打点
- [ ] positions_snapshots 打点
- [ ] 更新 Row 1.1 Dashboard query
- [ ] 更新 Row 3.1-3.3 Dashboard queries

### Phase 2: 尽快实施 (P1 - 交易分析)
- [ ] trading_signals 打点
- [ ] order_events 打点
- [ ] fills_live 打点
- [ ] exec_events 打点
- [ ] 更新 Row 2.1-2.3 Dashboard queries
- [ ] 更新 Row 4.1-4.3 Dashboard queries

### Phase 3: 增强功能 (P2 - 高级监控)
- [ ] feature_baselines 建立
- [ ] feature_snapshots 打点
- [ ] 更新 Row 2.4 Dashboard query

---

## 🔧 测试与验证

### 测试每个新打点

```bash
# 1. 检查数据是否写入
curl "http://localhost:8123/?database=trading" --data-binary "
SELECT count() FROM equity_pnl_snapshots WHERE date = today()
"

# 2. 检查最新记录
curl "http://localhost:8123/?database=trading" --data-binary "
SELECT * FROM equity_pnl_snapshots ORDER BY ts DESC LIMIT 5 FORMAT Pretty
"

# 3. 测试 Dashboard query
curl "http://localhost:8123/?database=trading" --data-binary "
SELECT
    anyLast(equity) - anyFirst(equity) as total_pnl
FROM equity_pnl_snapshots
WHERE date = today()
FORMAT Pretty
"
```

### 更新 Dashboard Panel

1. 打开 Grafana: `http://localhost:3000`
2. 找到 AI Boss Dashboard
3. 编辑占位符 panel
4. 替换为真实 query
5. 保存并测试

---

## 📞 支持

如有问题:
1. 查看 IMPLEMENTATION_STATUS.md - 当前实施状态
2. 查看 DATA_AVAILABILITY_REPORT.md - 数据源详情
3. 查看 DATA_GAP_ANALYSIS.md - 每个 panel 的详细需求

---

**Document Version**: 1.0
**Last Updated**: 2025-10-30 14:45 EDT
**Status**: Ready for implementation
