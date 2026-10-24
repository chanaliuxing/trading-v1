# Historical Data Loading Guide

## Quick Start

### Load 1 Day of Data
```bash
# Single symbol
venv/bin/python3 scripts/load_historical_data.py --symbol AAPL --days 1

# Multiple symbols
venv/bin/python3 scripts/load_historical_data.py --symbol AAPL MSFT NVDA --days 1
```

### Load 30 Days of Data
```bash
venv/bin/python3 scripts/load_historical_data.py --symbol AAPL MSFT NVDA GOOGL TSLA --days 30
```

### Load Specific Date Range
```bash
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL \
  --start 2024-01-01 \
  --end 2024-01-31
```

---

## Options

### Symbols
```bash
--symbol AAPL              # Single symbol
--symbol AAPL MSFT NVDA    # Multiple symbols
-s AAPL MSFT               # Short form
```

### Date Range

**Option 1: Days back from today**
```bash
--days 1      # Last 1 day
--days 7      # Last 7 days
--days 30     # Last 30 days
-d 30         # Short form
```

**Option 2: Specific dates**
```bash
--start 2024-01-01 --end 2024-01-31    # January 2024
--start 2024-12-01 --end 2024-12-31    # December 2024
```

### Bar Size
```bash
--bar-size "5 secs"    # 5-second bars (default)
--bar-size "1 min"     # 1-minute bars
--bar-size "5 mins"    # 5-minute bars
--bar-size "1 hour"    # 1-hour bars
--bar-size "1 day"     # Daily bars
```

### Trading Hours
```bash
--use-rth              # Regular trading hours only (9:30am-4pm ET)
                       # Default: All hours (includes pre/post market)
```

### Features
```bash
--no-features          # Skip automatic feature calculation
                       # Default: Features are calculated automatically
```

### IB Gateway Port
```bash
--ib-port 4002         # Paper trading (default)
--ib-port 4001         # Live trading
```

---

## Examples

### Example 1: Load 7 Days for Trading Symbols
```bash
# Load last 7 days for your trading symbols
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL MSFT NVDA GOOGL TSLA \
  --days 7
```

**Output:**
```
================================================================================
Loading historical data for AAPL
  Period: 2024-12-01 to 2024-12-08
  Bar size: 5 secs
================================================================================

Fetching chunk: 2024-12-01 to 2024-12-02
  ✅ Inserted 4,680 bars

Fetching chunk: 2024-12-02 to 2024-12-03
  ✅ Inserted 4,680 bars

...

✅ Completed AAPL: 32,760 total bars loaded

📊 SUMMARY
  Symbols loaded: AAPL, MSFT, NVDA, GOOGL, TSLA
  Total bars: 163,800
  Period: 2024-12-01 to 2024-12-08
```

### Example 2: Load Specific Month for Backtesting
```bash
# Load all of January 2024 for AAPL
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL \
  --start 2024-01-01 \
  --end 2024-01-31
```

### Example 3: Load RTH Only (for Strategies Trading During Market Hours)
```bash
# Load regular trading hours only
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL MSFT \
  --days 30 \
  --use-rth
```

### Example 4: Load Without Feature Calculation
```bash
# Load raw bars only, calculate features later
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL \
  --days 1 \
  --no-features
```

---

## Data Flow

1. **Fetch from IB Gateway**
   - Connects to IB Gateway (default: localhost:4002)
   - Requests historical bars via IB API
   - Respects IB rate limits (2-second delay between chunks)

2. **Insert into ClickHouse**
   - Bars stored in `bars_5s` table
   - Includes OHLCV + metadata (trades, vwap)

3. **Calculate Features** (if enabled)
   - Technical indicators computed automatically
   - Stored in `features_5s` table
   - Includes: EMAs, ATR, volume metrics, z-scores, etc.

---

## Verify Data

### Check Bars in ClickHouse
```bash
# Count bars per symbol
clickhouse-client --query "
SELECT
    symbol,
    COUNT(*) as bars,
    MIN(ts_start) as first_bar,
    MAX(ts_end) as last_bar
FROM bars_5s
GROUP BY symbol
ORDER BY symbol"
```

### Check Features
```bash
# Sample features for AAPL
clickhouse-client --query "
SELECT * FROM features_5s
WHERE symbol = 'AAPL'
ORDER BY ts DESC
LIMIT 10"
```

### Query Specific Date Range
```bash
clickhouse-client --query "
SELECT
    symbol,
    ts_start,
    open, high, low, close,
    volume
FROM bars_5s
WHERE symbol = 'AAPL'
  AND ts_start >= '2024-01-01 00:00:00'
  AND ts_start < '2024-01-02 00:00:00'
ORDER BY ts_start
LIMIT 20"
```

---

## Important Notes

### IB API Limits
- **Historical Data Pacing**: Max 60 requests per 10 minutes
- **Bar Count Limits**: Max ~2000 bars per request
- The script automatically chunks requests to stay within limits
- Uses 2-second delays between requests

### Data Availability
- **5-second bars**: Limited availability (typically last few months)
- **1-minute bars**: Better historical coverage
- **Daily bars**: Years of history available

### Trading Hours
- **use_rth=False** (default): Includes pre-market (4am-9:30am) + RTH + after-hours (4pm-8pm)
- **use_rth=True**: RTH only (9:30am-4pm ET)

### Feature Calculation
- Features require minimum lookback periods:
  - 20-period features: Need at least 20 bars
  - 50-period features: Need at least 50 bars
- First bars may have NULL features (insufficient history)

---

## Troubleshooting

### "No data returned for this chunk"
- **Cause**: IB doesn't have data for that period/symbol
- **Solution**: Try different date range or bar size

### "Connection refused"
- **Cause**: IB Gateway not running
- **Solution**: Start IB Gateway and ensure API is enabled

### "Rate limit exceeded"
- **Cause**: Too many requests to IB
- **Solution**: Script handles this automatically with delays

### "Features have NULL values"
- **Cause**: Insufficient historical data for indicator calculation
- **Solution**: Load more historical data (longer date range)

---

## Advanced Usage

### Load Data in Background
```bash
nohup venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL MSFT NVDA GOOGL TSLA \
  --days 30 \
  > /tmp/historical_load.log 2>&1 &

# Monitor progress
tail -f /tmp/historical_load.log
```

### Load Multiple Periods Separately
```bash
# Load Q1 2024
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL \
  --start 2024-01-01 --end 2024-03-31

# Load Q2 2024
venv/bin/python3 scripts/load_historical_data.py \
  --symbol AAPL \
  --start 2024-04-01 --end 2024-06-30
```

### Recalculate Features Only
If you already have bars but need to recalculate features:
```bash
venv/bin/python3 -c "
from services.market.features.feature_calculator import FeatureCalculator
from datetime import datetime

calc = FeatureCalculator(resolution='5s')
calc.compute_features_for_range(
    start_time=datetime(2024, 1, 1),
    end_time=datetime(2024, 1, 31),
    symbols=['AAPL', 'MSFT']
)
print('Features recalculated!')
"
```

---

## Performance Tips

1. **Load in Chunks**: Don't try to load years of 5-second data at once
2. **Use Larger Bar Sizes**: For backtesting, consider 1-min or 5-min bars
3. **Parallel Loads**: Run multiple scripts for different symbols in parallel
4. **Off-Peak Hours**: Load during non-market hours to avoid IB API congestion

---

## Next Steps

After loading historical data:

1. **Verify Data Quality**
   ```bash
   clickhouse-client --query "
   SELECT
       symbol,
       COUNT(*) as bar_count,
       AVG(volume) as avg_volume,
       SUM(CASE WHEN volume = 0 THEN 1 ELSE 0 END) as zero_vol_bars
   FROM bars_5s
   GROUP BY symbol"
   ```

2. **Backtest Your Strategy**
   - Use the loaded data with StrategyEngine.run_backtest()
   - See HOW_TO_USE.md for strategy examples

3. **Monitor Data Gaps**
   - Check for missing periods
   - Use backfill_service.py to fill gaps automatically

---

**Happy Backtesting!** 📊
