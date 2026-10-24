# Trading System - User Guide

## 🚀 Quick Start

### 1. Start the Trading System

```bash
# Navigate to project directory
cd /workspace/Documents/trading

# Activate virtual environment
source venv/bin/activate

# Load environment variables
source .env

# Start the system
python3 run_system.py
```

The system will run continuously until you stop it with `Ctrl+C`.

### 2. Verify System is Running

```bash
# Check process
ps aux | grep run_system.py

# Monitor logs
tail -f /tmp/trading_system.log

# Check IB Gateway connections
# You should see 2 active connections (Client IDs 17 & 27) in IB Gateway API
```

---

## 📊 System Overview

### Active Services

1. **Market Data Service** - Connects to IB Gateway, streams real-time ticks
2. **5-Second Aggregator** - Aggregates ticks into 5-second bars → Kafka
3. **Feature Calculator** - Computes technical indicators → ClickHouse
4. **Strategy Engine** - Executes your trading strategies

### Data Flow

```
IB Gateway → Market Data Service → 5s Aggregator → Kafka (md.bar.5s.v2)
                                                      ↓
                                              Feature Calculator
                                                      ↓
                                               ClickHouse (features_5s)
                                                      ↓
                                              Strategy Engine
```

---

## 🎯 Creating Trading Strategies

This system uses a configuration-driven loading mechanism that requires no changes to core code. Default flow is “pure config” (no decorator). You create a strategy class, then enable it in `config.yml` via `module` and `class`.

### Step 1: Create the Strategy File (no decorator required)

Create a Python file under `strategies/`, and define a class inheriting `libs.strategy.base.StrategyAlgorithm`. In `initialize()` subscribe to data using the context API.

Example: `strategies/my_awesome_strategy.py`

```python
from typing import Dict, Any
from libs.strategy import (
    StrategyAlgorithm,
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id,
)

class MyAwesomeStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        # Subscribe to feature stream (required)
        await self.ctx.subscribe_features(["AAPL", "MSFT"], bar_sec=60)

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        # Minimal example decision
        px = float(features.get("close", 0) or 0)
        if px > 0 and features.get("zscore_20", 0) < -2:
            env = DecisionEnvelope(
                decision_id=make_decision_id(),
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                ts_bar_end_us=self.ctx.now_us(),
                target_qty=100.0,
                order_style=OrderStyle(kind="MKT"),
                freshness=Freshness(ttl_ms=5000, dlb_ms=500),
                extras={"ref_px": px},
            )
            await self.ctx.submit_intent(env)
```

Important:
- Use `await self.ctx.subscribe_features(...)` in `initialize()`; do not call internal engine methods directly.
- Import base classes from `libs.strategy`, not from `services/strategy/strategy_base.py` (legacy).

### Step 2: Enable it in `config.yml` (config-driven, default)

The loader reads `strategies:` from `config.yml`. Provide `module` and `class`, set `enabled: true`, and pass parameters under `params`.

```yaml
# In config.yml

strategies:
  - id: "MyAwesomeStrategy-001"          # Unique instance id
    enabled: true
    module: "strategies.my_awesome_strategy"  # Python module path
    class: "MyAwesomeStrategy"                # Class name
    params:
      cadence_s: 10
      ttl_ms: 5000
      extras:
        symbols: ["TSLA", "NVDA"]
        capital_limit_usd: 25000
        # other custom parameters...
```

Optional: decorator + registry
- You may add `@register_strategy("my-awesome-strategy-v1")` and map it in `strategy_registry:`.
- Current loader still requires `module` and `class`. You can supply a custom `registry_name` in each strategy entry if you need a specific registry alias.

```yaml
strategy_registry:
  my-awesome-strategy-v1: strategies.my_awesome_strategy.MyAwesomeStrategy

strategies:
  - id: "MyAwesomeStrategy-001"
    enabled: true
    module: "strategies.my_awesome_strategy"
    class: "MyAwesomeStrategy"
    registry_name: "my-awesome-strategy-v1"   # optional alias
    params:
      extras:
        symbols: ["AAPL"]
```

### How It Works

At startup the system:
1. Reads `strategy_registry:` to pre-register any explicit mappings (optional).
2. Reads `strategies:`; for each enabled item it imports `module` and `class` and, if needed, registers an alias via `registry_name` or falls back to `id`.
3. Creates a `StrategyContext`, calls your strategy’s `initialize()`, and then routes feature updates to `on_features()` when new bars arrive.

This config-first approach is flexible and allows adding strategies without modifying core services.

---

## Enable RevF (Engine‑Driven, No Kafka)

RevF runs via `on_features()` with 1m features from the engine and optionally consumes SFS 1s/5s for intra‑bar exits. No internal Kafka consumers are used.

```yaml
# In config.yml

strategies:
  - id: "RevF-001"
    enabled: true
    module: "strategies.reve_intraday_strategy"
    class: "RevEIntradayStrategy"
    params:
      ttl_ms: 5000
      extras:
        symbols: ["AAPL", "TSLA", "NVDA"]
```

Notes
- Ensure `services.sfs_1s.enabled=true` and `services.sfs_5s.enabled=true` when you need intra‑minute micro‑path execution.
- The 1m tumbling MV from SFS 5s is provided by `schema/clickhouse/sfs_1m_tumble.sql` and auto‑applied at startup.

---

## ⚡ SFS (1s/5s) Low‑Latency Features

SFS provides ultra‑low‑latency microstructure data for strategies.

What you get
- 1s microbars in Redis (ring buffer, TTL 30–60 min)
- 5s sliding bars (1‑second stride) in ClickHouse `sfs.sfs_5s`
- Prometheus metrics and Grafana queries for rate/latency/size

### 1) Enable in `config.yml`

```yaml
services:
  # 1s microbars → Redis (ring buffer)
  sfs_1s:
    enabled: true
    ttl_sec: 3600          # keep ~1h in Redis
    flush_delay_ms: 250    # allow late ticks
    group_id: sfs_1s_agg
    topic: md.tick.trade.v1

  # 5s sliding bars (1s stride) → ClickHouse sfs.sfs_5s
  sfs_5s:
    enabled: true
    flush_period_ms: 1000  # 1 row/sec per symbol
    lookback_n: 5          # 5 × 1s window
    group: sfs_5s
    # symbols: [AAPL, TSLA]  # optional whitelist (defaults to universe)
```

Notes
- The writer auto‑creates the `sfs` database and `sfs.sfs_5s` table on start.
- Start small (2–5 symbols) to validate, then expand to full universe.

### 2) Start the system

```bash
python run_system.py
```

### 3) Verify data

Redis (1s microbars):
```bash
redis-cli LRANGE sfs:1s:AAPL 0 4
```

ClickHouse (5s sliding):
```sql
SELECT count(*)
FROM sfs.sfs_5s
WHERE symbol='AAPL'
  AND ts >= today();
```

Metrics (Prometheus on port 9108):
- 1s: `sfs1s_ticks_total`, `sfs1s_bars_flushed_total`, `sfs1s_flush_latency_ms`
- 5s: `sfs5s_rows_written_total`, `sfs5s_flush_latency_ms`, `sfs5s_errors_total`

See: `doc/dashboard/SFS_1S_MONITORING.md`, `doc/dashboard/SFS_5S_MONITORING.md`

### 4) Use in a strategy

Read 1s microbars (Redis) alongside 1m features to confirm microstructure:

```python
from typing import Dict, Any
from libs.strategy import StrategyAlgorithm, DecisionEnvelope, OrderStyle, Freshness, make_decision_id
from libs.dal.sfs_facade import SFS1sReader

class MyStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        await self.ctx.subscribe_features(["AAPL"], bar_sec=60)
        self.sfs = SFS1sReader()

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        sub = await self.sfs.last_n(symbol, 20)   # newest→older 1s bars
        # ... combine with 1m features, then submit intent ...
```

Sample strategy: `strategies/sfs_fusion_demo.py`
- Enable by adding to `config.yml` → `strategies:` with `module: strategies.sfs_fusion_demo`, `class: SfsFusionDemo`.

### 5) Higher granularities (1m/5m/…)

For 1m or longer periods, use ClickHouse materialized views from `sfs.sfs_5s`. See `doc/sfs_higher_granularity.md` for ready‑to‑run DDL/MV and backfill SQL.

---

## 📈 Available Features for Strategies

When you receive `features` in `on_features()`, you have access to:

### Price Features
- `close` - Close price
- `vwap` - Volume-weighted average price
- `ema_10`, `ema_20`, `ema_50` - Exponential moving averages

### Volatility Features
- `atr_14`, `atr_5` - Average True Range (14 & 5 periods)
- `z_price_20` - Price z-score (20-period)

### Volume Features
- `volume` - Current volume
- `vol_ema_20` - Volume EMA (20-period)
- `vol_spike` - Volume spike ratio (current / average)
- `z_vol_20` - Volume z-score

### Microstructure Features
- `spread_bps` - Bid-ask spread in basis points

### Regression Features
- `slope_20` - Linear regression slope (20-period)
- `r2_20` - R-squared coefficient of determination

---

## 🤖 Hyperparameter Optimization (HPO)

This project includes a powerful, Optuna-based HPO framework for finding the optimal parameters for your trading strategies. It replaces traditional grid search with more efficient algorithms like Bayesian Optimization.

### Key Features
- **Advanced Algorithms**: Utilizes Bayesian search (TPE, BoTorch) to intelligently find optimal parameters in fewer trials.
- **Parallel & Persistent**: Supports multi-core processing (`--n-jobs`) and can save/resume optimization studies using a database backend (`--storage`).
- **Pruning**: Automatically stops unpromising trials early to save time.

### How to Use
The new HPO system is run via the `hpo_cli_upgraded.py` script. You configure a search space in your JSON config file and launch the study from the command line.

For a complete walkthrough, command-line options, and configuration examples, please refer to the full guide:
- **[HPO 2.0: Bayesian Optimization Framework Guide](config/HPO_USAGE_GUIDE.md)**

---

## ⚙️ Configuration

### Environment Variables (`.env`)

```bash
# Trading Mode
TRADING_MODE=shadow          # shadow, paper, or live
SHADOW_SYMBOLS=AAPL,MSFT,NVDA

# IB Gateway
ENV_MODE=paper              # paper or live
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_LIVE_PORT=4001
IB_CLIENT_ID=7

# Infrastructure
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
STATEBUS_REDIS=redis://127.0.0.1:6379/0

# Database
PG_HOST=localhost
PG_PORT=5432
PG_DB=test
PG_USER=postgres
PG_PASSWORD=postgres
```

### Symbol Configuration

```sql
-- Enable/disable symbols
UPDATE symbol_config
SET enabled = TRUE
WHERE symbol = 'AAPL';

-- Set symbol parameters
UPDATE symbol_config
SET max_notional_usd = 50000,
    tick_size = 0.01
WHERE symbol = 'AAPL';
```

---

## 🛡️ Trading Modes

### Current Mode: Paper Trading ✅
The system is configured for **paper trading** with IB paper account.

### 1. Shadow Mode (Testing Phase)
- **Purpose**: Test strategy logic without real orders
- **Capital**: 0% (no actual trading)
- **Orders**: Logged to `shadow_orders` table only
- **Use Case**: Initial strategy development and backtesting

```bash
TRADING_MODE=shadow
TRADING_SYMBOLS=AAPL,MSFT,NVDA
ENV_MODE=paper
```

### 2. Paper Mode (Current - Validation Phase) ✅
- **Purpose**: Validate execution with IB paper account
- **Capital**: IB paper account balance
- **Orders**: Sent to IB paper trading (real execution, fake money)
- **Use Case**: Strategy validation before going live

```bash
TRADING_MODE=paper
TRADING_SYMBOLS=AAPL,MSFT,NVDA,GOOGL,TSLA
ENV_MODE=paper
IB_PAPER_PORT=4002
```

### 3. Live Mode (Production - Use with Caution ⚠️)
- **Purpose**: Real money trading
- **Capital**: Real account balance
- **Process**: Canary deployment (5% → 25% → 50% → 100%)
- **Requirements**: Extensive paper testing, risk management validated

```bash
TRADING_MODE=live
TRADING_SYMBOLS=AAPL,MSFT  # Start with limited symbols
ENV_MODE=live
IB_LIVE_PORT=4001
```

---

## 📊 Monitoring

### View Strategy Performance

```sql
-- Check shadow orders
SELECT symbol, side, qty, limit_px, timestamp, rationale
FROM shadow_orders
WHERE strategy_id = 'mean_rev_001'
ORDER BY timestamp DESC
LIMIT 20;

-- Check positions
SELECT * FROM core_position
WHERE strategy_id = 'mean_rev_001'
AND qty != 0;

-- Check P&L
SELECT
    strategy_id,
    reserved_usd,
    realized_pnl_usd
FROM core_strategy_ledger
WHERE strategy_id = 'mean_rev_001';
```

### Monitor Market Data

```bash
# Check Kafka messages
kafka-console-consumer --bootstrap-server localhost:9092 \
    --topic md.bar.5s.v2 \
    --from-beginning \
    --max-messages 10

# Query features in ClickHouse
clickhouse-client --query "
SELECT * FROM features_5s
WHERE symbol = 'AAPL'
ORDER BY ts DESC
LIMIT 10"
```

### System Health

```bash
# Check service status
ps aux | grep run_system.py

# Monitor logs
tail -f /tmp/trading_system.log | grep -E "(error|Error|ERROR)"

# Check IB connections
# IB Gateway → API → Active Sessions
# Should see 2 connections (Client IDs 17 & 27)
```

---

## 🔧 Advanced Configuration

### Adjust Feature Calculation

Edit `services/market/features/feature_calculator.py`:

```python
# Change lookback windows
self.atr_window = 14  # Change ATR period
self.rsi_window = 14  # Change RSI period

# Add custom features
# Modify _build_feature_sql() to add your calculations
```

### Custom Bar Resolutions

Currently supports:
- 5-second bars (`features_5s`)
- 10-second bars (`features_10s`)

To add more resolutions, create new tables in ClickHouse and configure in `feature_calculator.py`.

### Strategy Parameters

```python
class StrategyParams:
    symbols: List[str]        # Symbols to trade
    cadence_s: int           # Execution frequency (seconds)
    capital_limit_usd: float # Max capital to use
    max_position_pct: float  # Max position size (% of portfolio)
    # Add your custom parameters
```

---

## 🚨 Emergency Procedures

### Kill Switch (Stop All Trading)

```sql
-- Disable all strategies
UPDATE core_strategy SET enabled = FALSE;

-- Disable all symbols
UPDATE symbol_config SET enabled = FALSE;
```

### Stop System

```bash
# Graceful shutdown (Ctrl+C in terminal)
# Or force kill:
pkill -9 -f run_system.py
```

### Reconnect to IB

System auto-reconnects if IB Gateway disconnects. Manual restart:

```bash
# Kill system
pkill -9 -f run_system.py

# Restart
python3 run_system.py
```

---

## 📚 Next Steps

1. **Create Your First Strategy**
   - Copy the mean reversion example above
   - Modify the logic for your strategy
   - Test in shadow mode

2. **Monitor Performance**
   - Check `shadow_orders` table daily
   - Analyze P&L and win rate
   - Refine strategy parameters

3. **Progress Through Modes**
   - Shadow (2 weeks) → Paper (2 weeks) → Live (canary)
   - Only advance when strategy shows consistent profitability

4. **Scale Up**
   - Add more symbols
   - Increase capital allocation
   - Deploy multiple strategies

---

## 🔗 Additional Resources

- **Architecture Docs**: `doc/` folder (78 design documents)
- **System Guide**: `TRADING_SYSTEM_GUIDE.md`
- **Quick Reference**: `QUICK_START.md`
- **Database Schema**: `schema/` folder

---

## ❓ FAQ

**Q: Why is the system "stuck" after starting?**
A: It's not stuck! The system runs in a keep-alive loop, silently processing data in the background. This is normal operation.

**Q: How do I know if data is flowing?**
A: Check Kafka topic (`md.bar.5s.v2`) or ClickHouse (`features_5s` table). You should see new records every 5 seconds.

**Q: Can I run multiple strategies simultaneously?**
A: Yes! Each strategy has a unique `strategy_id`. Load multiple strategies in the StrategyEngine.

**Q: How do I backtest a strategy?**
A: Use `StrategyEngine.run_backtest()` with historical date range.

**Q: What happens if IB Gateway goes down?**
A: System auto-reconnects with exponential backoff (5s → 300s max). Data collection resumes when connection restores.

---

**System Status**: ✅ Running in Paper Trading Mode (IB Paper Account)

Happy Trading! 📈