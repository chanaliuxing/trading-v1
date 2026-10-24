# Backtest System Guide

## Overview

Your trading system has **two backtest engines**:

1. **Root Backtest Engine** (`backtest_engine.py`) - ClickHouse-based, vectorized
2. **Services Backtest Engine** (`services/backtest/backtest_engine.py`) - DataFrame-based, research-friendly

Both support async strategy API and include simulated broker (`sim_adapter.py`).

---

## Quick Start

### Simple Backtest (Last 30 Days)

```bash
python run_backtest_simple.py --symbol TSLA --days 30
```

### Custom Date Range

```bash
python run_backtest_simple.py \
  --symbol NVDA \
  --start 2025-04-01 \
  --end 2025-10-01 \
  --cash 100000 \
  --commission 0.0035
```

### With Custom Config

```bash
python run_backtest_simple.py \
  --symbol AAPL \
  --days 60 \
  --config strategies/configs/aapl_v7_2.json \
  --output results/aapl_backtest.json
```

---

## Prerequisites

### Data Requirements

The backtest engines expect **features data** in ClickHouse:

**Required Tables:**
- `features_1m` - 1-minute features (recommended for V7.2 strategies)
- `features_5m` - 5-minute features (optional, for longer timeframes)

**Required Columns:**
- `ts` - Timestamp
- `symbol` - Symbol
- `open`, `high`, `low`, `close` - OHLC prices
- `volume` - Volume
- `vwap` - Volume-weighted average price
- `atr_14` - Average True Range (14 periods)
- `ema_10`, `ema_20`, `ema_50` - EMAs
- `spread_bps` - Bid-ask spread
- Other technical indicators as needed

### Check If You Have Features Data

```sql
-- Check 1m features
clickhouse-client --database trading --query "
SELECT symbol, count(*) as rows, min(ts) as earliest, max(ts) as latest
FROM features_1m
WHERE symbol = 'TSLA'
GROUP BY symbol
"

-- Check 5m features
clickhouse-client --database trading --query "
SELECT symbol, count(*) as rows, min(ts) as earliest, max(ts) as latest
FROM features_5m
WHERE symbol = 'TSLA'
GROUP BY symbol
"
```

### Current Data Status

Based on our data coverage check:

✅ **Bars Data (Ready):**
- All 9 main symbols have 180+ days of 1m and 5m bars
- SPY: 193 days of 5m bars

⚠️ **Features Data:**
- Need to compute features from bars data
- Features calculator available: `services/market/features/feature_calculator.py`

---

## Backtest Engine Options

### Option 1: Services Backtest Engine (Recommended for Research)

**Location:** `services/backtest/backtest_engine.py`

**Features:**
- DataFrame-based (can load from CSV/Parquet)
- Research-friendly
- Point-in-time data leakage protection
- Portfolio overlay support
- Simpler data requirements

**Usage:**

```python
from services.backtest.backtest_engine import BacktestEngine, BacktestConfig

config = BacktestConfig(
    symbols=["TSLA"],
    start="2025-04-01 09:30:00",
    end="2025-10-01 16:00:00",
    strategy_path="strategies/tsla_v7_2.py",
    strategy_class="TSLASimplifiedV7_2",
    strategy_params={"symbol": "TSLA"},
    starting_cash=100000.0,
    commission_per_share=0.0035,
    slippage_bps=1.0,
)

engine = BacktestEngine(config)
result = await engine.run()
```

### Option 2: Root Backtest Engine (Production-Grade)

**Location:** `backtest_engine.py`

**Features:**
- ClickHouse integration
- Vectorized bar-by-bar execution
- Full tracing support (OpenTelemetry)
- Portfolio overlay
- Production-ready metrics

**Usage:**

```bash
python backtest_engine.py \
  --symbols TSLA \
  --start "2025-04-01 09:30:00" \
  --end "2025-10-01 16:00:00" \
  --timeframe 1m \
  --features_table features_1m \
  --strategy_path "strategies.tsla_v7_2.TSLASimplifiedV7_2" \
  --initial_cash 100000 \
  --commission_per_share 0.0035
```

---

## Preparing Features Data

If features tables don't exist, you need to compute them from bars data:

### Step 1: Check Available Feature Calculator

```bash
# Look for feature calculator
ls -la services/market/features/feature_calculator.py
```

### Step 2: Create Features Table (if needed)

```sql
-- Create 1m features table based on bars_1m_ibkr
CREATE TABLE IF NOT EXISTS trading.features_1m
(
    ts DateTime,
    symbol String,
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume UInt64,
    vwap Nullable(Float64),
    atr_14 Nullable(Float64),
    ema_10 Nullable(Float64),
    ema_20 Nullable(Float64),
    ema_50 Nullable(Float64),
    spread_bps Nullable(Float64),
    -- Add other features as needed
    INDEX idx_symbol symbol TYPE bloom_filter GRANULARITY 1,
    INDEX idx_ts ts TYPE minmax GRANULARITY 1
)
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts);
```

### Step 3: Compute Features from Bars

```python
# Example: Compute features from bars_1m_ibkr
import pandas as pd
from libs.db.clickhouse import ClickHouseDB

ch = ClickHouseDB()

# Load bars
query = """
    SELECT ts_start as ts, symbol, open, high, low, close, vwap, volume
    FROM bars_1m_ibkr
    WHERE symbol = 'TSLA'
      AND ts_start >= '2025-04-01 09:30:00'
      AND ts_start <= '2025-10-01 16:00:00'
    ORDER BY ts_start
"""
df = pd.DataFrame(ch.query(query))

# Compute technical indicators
df['ema_10'] = df['close'].ewm(span=10).mean()
df['ema_20'] = df['close'].ewm(span=20).mean()
df['ema_50'] = df['close'].ewm(span=50).mean()

# ATR calculation
high_low = df['high'] - df['low']
high_close = (df['high'] - df['close'].shift()).abs()
low_close = (df['low'] - df['close'].shift()).abs()
tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
df['atr_14'] = tr.ewm(span=14).mean()

# Insert into features_1m
ch.client.insert_df('features_1m', df)
```

---

## Backtest Configuration

### Strategy Config Structure

Your strategies use JSON config files in `strategies/configs/`:

```json
{
  "symbol": "TSLA",
  "windows": {
    "short_days": 7,
    "long_days": 60,
    "baseline_days": 180
  },
  "regime": {
    "epsilon": 0.0048,
    "market_mult": {"BULL": 1.0, "NEUTRAL": 0.75, "BEAR": 0.5},
    "vix_symbol": "VIX"
  },
  "pause": {
    "theta_vix": 0.12,
    "theta_spread": 0.0007,
    "theta_atr": 2.1
  },
  "positioning": {
    "base_unit_pct": 0.018,
    "max_concurrent": 2
  },
  "slippage": {
    "eps_p95": 0.0005,
    "min": 0.0002,
    "max": 0.0008
  },
  "commission_per_share": 0.0035
}
```

---

## Understanding Backtest Results

### Metrics Explained

```python
{
  "metrics": {
    "final_nav": 102500.0,           # Final portfolio value
    "cash": 85000.0,                 # Cash remaining
    "total_fee": 150.0,              # Total commissions paid
    "realized_pnl": 2350.0,          # Total realized P&L
    "trades": 45,                    # Number of trades
    "win_rate": 0.58,                # Win rate (58%)
    "profit_factor": 1.85            # Gross profit / Gross loss
  },
  "bars_processed": 8640             # Total bars simulated
}
```

### Good Backtest Criteria

✅ **Profitable**: `realized_pnl > 0`
✅ **Consistent**: `win_rate > 0.50`
✅ **Risk/Reward**: `profit_factor > 1.5`
✅ **Active**: `trades > 10` (not too few)
✅ **Reasonable**: `trades < 500` (not overtrading)

---

## Advanced Features

### Walk-Forward Optimization

Located in `wf_hpo.py` - Hyperparameter optimization with walk-forward validation.

```bash
# Run walk-forward HPO
python wf_hpo.py --config strategies/configs/tsla_v7_2.json
```

### Batch Backtesting

Test multiple symbols/configs in parallel:

```python
from services.backtest.backtest_engine import BacktestEngine

symbols = ["TSLA", "NVDA", "AAPL", "META"]
results = {}

for symbol in symbols:
    config = BacktestConfig(
        symbols=[symbol],
        start="2025-04-01 09:30:00",
        end="2025-10-01 16:00:00",
        # ... other config
    )
    engine = BacktestEngine(config)
    results[symbol] = await engine.run()
```

### Portfolio-Level Backtesting

Test multiple strategies together:

```python
config = BacktestConfig(
    symbols=["TSLA", "NVDA", "AAPL"],
    overlay={
        "max_drawdown_pct": 0.10,
        "max_leverage": 1.0,
        "risk_limits": {
            "max_position_pct": 0.20,
            "max_sector_pct": 0.40
        }
    },
    # ... other config
)
```

---

## Troubleshooting

### Issue: "No data returned"

**Cause:** Features table is empty or doesn't cover the date range

**Solution:**
1. Check data availability:
   ```sql
   SELECT min(ts), max(ts), count(*) FROM features_1m WHERE symbol = 'TSLA'
   ```
2. Compute features if missing (see "Preparing Features Data")

### Issue: "Strategy must implement on_features"

**Cause:** Strategy class doesn't have `on_features` method

**Solution:** Verify your strategy inherits from correct base class and implements required methods:
```python
async def on_features(self, symbol: str, features: Dict[str, Any]):
    # Your logic here
    pass
```

### Issue: "Cannot load strategy module"

**Cause:** Wrong path or class name

**Solution:**
- Use file path for services engine: `"strategies/tsla_v7_2.py"`
- Use module path for root engine: `"strategies.tsla_v7_2.TSLASimplifiedV7_2"`

---

## Next Steps

1. **Compute Features**: Generate `features_1m` from your `bars_1m_ibkr` data
2. **Run Test Backtest**: Start with a small date range (7 days) to validate setup
3. **Full Backtest**: Run 30-180 day backtest for robust statistics
4. **Optimize**: Use walk-forward HPO to tune parameters
5. **Validate**: Compare backtest results with paper trading

---

## Resources

- **Backtest Docs**: `doc/backtest/` folder
- **Strategy Examples**: `strategies/` folder
- **Config Examples**: `strategies/configs/` folder
- **Test Cases**: `tests/test_backtest.py`

---

**Last Updated**: 2025-10-14
