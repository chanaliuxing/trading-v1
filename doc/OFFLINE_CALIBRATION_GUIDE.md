# Offline Parameter Calibration Tool - User Guide

## Overview

The **Offline Parameter Calibration Tool** (`tools/offline_calibrator.py`) extracts the "Parameter Brain" logic from the V7.2 strategy into a standalone utility. This allows you to:

1. **Pre-generate optimal parameters** for any symbol using historical data
2. **Test parameter sensitivity** without running live strategies
3. **Batch-calibrate** all universe symbols before deployment
4. **Archive parameter history** for backtesting and optimization

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   Offline Calibrator                        │
│                                                             │
│  1. Fetch Historical Data (ClickHouse)                     │
│     ├─ 1m bars (last 35 days, RTH only)                   │
│     └─ 5m bars (last 60 days, RTH only)                   │
│                                                             │
│  2. Calculate Core Parameters                              │
│     ├─ A1: thr_sigma (P60 of VWAP deviation)              │
│     ├─ A1: RVOL range (P30-P80 of intraday RVOL)          │
│     ├─ A1: time_exit_min (median return-to-VWAP time)     │
│     ├─ A2: grid_g (P28 of 5m true range)                  │
│     ├─ B1: rvol_min (P40 of RVOL distribution)            │
│     └─ k_ATR (P90 of |ΔC_5m| / mean(ATR_5m))              │
│                                                             │
│  3. Anti-Starvation Check                                  │
│     └─ If estimated triggers < 50% of target:              │
│        ├─ Relax A1_thr_sigma (×0.9)                        │
│        └─ Relax A2_grid_g (×0.9)                           │
│                                                             │
│  4. Save to LKG File                                        │
│     └─ {symbol}_v7_2_lkg.json                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Usage Examples

### 1. Calibrate Single Symbol

```bash
# Basic usage - calibrate AAPL with 60 days of data
python tools/offline_calibrator.py --symbol AAPL --days 60

# Use NVDA as template (for high-volatility stocks)
python tools/offline_calibrator.py --symbol AMD --template NVDA --days 90

# Save to custom directory
python tools/offline_calibrator.py --symbol MSFT --output-dir strategies/configs/
```

**Output:**
```
================================================================================
Calibration Results for AAPL
================================================================================
A1 thr_sigma:     0.004500
A1 RVOL range:    0.82 - 1.75
A1 time_exit:     45 minutes
A2 grid_g:        0.8500
A2 grid_g_floor:  0.6000
k_ATR:            1.8500
B1 ORB length:    30 minutes
B1 RVOL min:      0.85
================================================================================
```

**Generated File:** `aapl_v7_2_lkg.json`

---

### 2. Batch Calibrate All Universe Symbols

```bash
# Calibrate all tradeable symbols (excludes SPY, VIX, SQQQ)
python tools/offline_calibrator.py --all --days 90

# This will generate LKG files for:
# - AAPL, TSLA, NVDA, MSFT, AMD, META, AMZN, GOOGL, QQQ
# - TQQQ, VOO, ORCL, INTC, PLMR, ELF
```

**Automated Exclusions:**
- `SPY`, `VIX`, `SQQQ` → Reference indices only, not tradeable strategies

---

### 3. Use Different Templates

The tool supports different templates for different volatility profiles:

| Template | Best For | Typical Symbols |
|----------|----------|-----------------|
| `TSLA` (default) | High volatility tech/growth | TSLA, NVDA, AMD |
| `AAPL` | Mega-cap stable stocks | AAPL, MSFT, GOOGL |
| `QQQ` | Low volatility ETFs | QQQ, VOO, SPY |

```bash
# Use AAPL template for stable mega-cap
python tools/offline_calibrator.py --symbol MSFT --template AAPL

# Use TSLA template for volatile semiconductor
python tools/offline_calibrator.py --symbol AMD --template TSLA
```

---

## Parameter Calculation Logic

### A1: VWAP Deviation Threshold (`thr_sigma`)

**Purpose:** Trigger long entries when price dips below VWAP by this threshold.

**Calculation:**
1. Compute session VWAP from 1m bars
2. Calculate deviation: `(close - VWAP) / VWAP`
3. Take P60 of absolute deviations
4. Clip to bounds: `[0.004, 0.020]`

**Interpretation:**
- **Lower values** (0.004-0.006): More sensitive, triggers on small dips (stable stocks)
- **Higher values** (0.010-0.020): Less sensitive, waits for bigger dips (volatile stocks)

**Example:**
- AAPL (stable): `0.0045` → triggers on -0.45% VWAP deviation
- NVDA (volatile): `0.0110` → triggers on -1.10% VWAP deviation

---

### A1: RVOL Range (`rvol_lo`, `rvol_hi`)

**Purpose:** Filter trades by relative volume to avoid low-liquidity periods.

**Calculation:**
1. Compute intraday RVOL (5m bars, 5-7 day lookback)
2. P30 → `rvol_lo` (clip: [0.75, 1.00])
3. P80 → `rvol_hi` (clip: [1.3, 2.5])

**Interpretation:**
- `rvol_lo = 0.85`: Only trade when volume ≥ 85% of typical
- `rvol_hi = 1.80`: Reduce size when volume > 180% of typical (panic/news)

---

### A1: Time Exit (`time_exit_min`)

**Purpose:** Force exit if price doesn't return to VWAP within this many minutes.

**Calculation:**
1. Track historical "below VWAP" episodes
2. Measure time until price returns to VWAP
3. Take median of all return times
4. Clip to bounds: `[25, 60]`

**Interpretation:**
- **25 min**: Fast mean-reversion (stable stocks)
- **60 min**: Slow mean-reversion (volatile stocks, allow more time)

---

### A2: Grid Spacing (`grid_g`)

**Purpose:** Dollar spacing between accumulation layers below SMA30.

**Calculation:**
1. Compute 5m true range (high-low, high-prevClose, prevClose-low)
2. Take P28 of true range distribution
3. Clip to bounds: `[0.6, 3.5]`

**Interpretation:**
- **$0.60-$1.00**: Tight grids (low volatility, e.g., QQQ)
- **$1.50-$2.50**: Wide grids (high volatility, e.g., NVDA)

**Example:**
- AAPL: `grid_g = 0.85` → Layer 1: -$0.85, Layer 2: -$1.70, etc.
- TSLA: `grid_g = 1.50` → Layer 1: -$1.50, Layer 2: -$3.00, etc.

---

### k_ATR: Stop Loss Multiplier

**Purpose:** Dynamic stop-loss based on recent volatility.

**Calculation:**
1. Compute 5m ATR(14)
2. Calculate P90 of `|ΔC_5m|` (absolute 5m price changes)
3. Ratio: `P90(|ΔC|) / mean(ATR)`
4. Map ratio to k_ATR range: `[1.4, 3.0]`

**Interpretation:**
- **1.4x ATR**: Tight stops (stable stocks, low noise)
- **2.5x ATR**: Wide stops (volatile stocks, avoid whipsaws)

---

### B1: ORB RVOL Min (`rvol_min`)

**Purpose:** Only trade Opening Range Breakouts during high volume.

**Calculation:**
1. Take P40 of RVOL distribution
2. Clip to bounds: `[0.75, 1.10]`

**Interpretation:**
- Ensures ORB trades only execute when volume is above median (reduces false breakouts)

---

## Anti-Starvation Logic

**Problem:** If parameters are too strict, the strategy may never trigger.

**Solution:** If estimated trigger rate < 50% of target (default: 4 per day):

1. **Relax `A1_thr_sigma`** by 10%:
   - Original: `0.0100` → Relaxed: `0.0090`
   - Effect: Triggers on smaller VWAP deviations

2. **Relax `A2_grid_g`** by 10%:
   - Original: `1.50` → Relaxed: `1.35`
   - Effect: Enters grid trades sooner

**When Applied:**
- Stocks with very low intraday volatility
- Symbols with limited historical data
- Newly added symbols without calibration history

---

## Workflow Integration

### Step 1: Generate Initial Parameters

```bash
# First-time calibration for new symbol
python tools/offline_calibrator.py --symbol ORCL --days 90
```

### Step 2: Copy LKG Values to Strategy Config

```bash
# Copy generated parameters to strategy config
cp orcl_v7_2_lkg.json strategies/configs/orcl_v7_2.json
```

### Step 3: Enable Config-Driven Mode

Edit `strategies/configs/orcl_v7_2.json`:

```json
{
  "symbol": "ORCL",
  "premarket_calibration": {
    "enabled": true,
    "use_config_values": true  // ← IMPORTANT: Use calibrated values
  },
  "A1": {
    "thr_sigma": 0.0048,  // From LKG file
    "rvol_lo": 0.82,
    "rvol_hi": 1.75,
    "time_exit_min": 45
  },
  "A2": {
    "grid_g": 0.85,
    "grid_g_floor": 0.60
  }
  // ... rest of config
}
```

### Step 4: Add to Universe and Deploy

1. Add `ORCL` to `config.universe` list
2. Restart system → automatic subscription + backfill
3. Strategy auto-loads config from JSON

---

## Troubleshooting

### Issue 1: "Insufficient historical data"

**Symptom:**
```
ERROR: No data found for ORCL in bars_1m_ibkr
```

**Cause:** Symbol not yet in ClickHouse database.

**Fix:**
1. Add symbol to `config.universe`
2. Restart system to trigger automatic backfill
3. Wait 1-2 hours for catchup service to populate historical data
4. Re-run calibration

---

### Issue 2: Parameters Hit Bounds

**Symptom:**
```
A1 thr_sigma: 0.004000  ← Clipped to MIN bound
A2 grid_g:    3.500000  ← Clipped to MAX bound
```

**Cause:** Symbol has extreme volatility characteristics.

**Fix:**

**Option A:** Adjust bounds in template config:
```json
{
  "premarket_calibration": {
    "bounds": {
      "A1_thr_sigma": {"min": 0.002, "max": 0.025},  // Wider range
      "A2_grid_g": {"min": 0.4, "max": 5.0}
    }
  }
}
```

**Option B:** Use different template:
```bash
# Switch from AAPL to TSLA template for volatile stock
python tools/offline_calibrator.py --symbol AMD --template TSLA
```

---

### Issue 3: Low Estimated Trigger Rate

**Symptom:**
```
Estimated triggers per day: 0.8 (target: 4)
WARNING: Low trigger rate detected, applying anti-starvation relaxation
```

**Cause:** Parameters too conservative for symbol's trading patterns.

**Fix:**

**Automatic:** Tool applies 10% relaxation automatically.

**Manual:** Further adjust in config:
```json
{
  "anti_starvation": {
    "relax_sigma": [0.85, 0.75],  // 15% then 25% relaxation
    "relax_g": [0.85, 0.75]
  }
}
```

---

### Issue 4: Pandas Deprecation Warning

**Symptom:**
```
FutureWarning: Series.fillna with 'method' is deprecated
```

**Status:** Fixed in latest version (line 58 uses `.ffill()` instead).

**Verify Fix:**
```bash
grep -n "fillna(method=" tools/offline_calibrator.py
# Should return nothing
```

---

## Best Practices

### 1. Calibration Frequency

| Scenario | Recommended Frequency |
|----------|----------------------|
| New symbol | Before first deployment |
| Existing symbol | Monthly (or after major volatility regime change) |
| After stock split | Immediately after split |
| Earnings season | Week before earnings (optional) |

### 2. Data Requirements

| Resolution | Minimum Days | Recommended Days | Max Days |
|------------|--------------|------------------|----------|
| 1m bars | 7 days | 30 days | 35 days |
| 5m bars | 30 days | 60 days | 90 days |

**Why 35-day cap on 1m data?**
- ClickHouse query performance (millions of rows)
- Most recent data more relevant for calibration
- Reduces memory footprint

### 3. Template Selection Guide

```python
# Decision tree for template selection
if symbol in ["AAPL", "MSFT", "GOOGL", "AMZN"]:
    template = "AAPL"  # Mega-cap stable
elif symbol in ["NVDA", "AMD", "TSLA"]:
    template = "TSLA"  # High volatility tech
elif symbol in ["QQQ", "VOO", "SPY"]:
    template = "QQQ"   # Low volatility ETF
else:
    template = "TSLA"  # Default (safe conservative)
```

### 4. Validation Checklist

After calibration, verify:

- [ ] `A1_thr_sigma` in reasonable range (0.004-0.015)
- [ ] `A2_grid_g` > `A2_grid_g_floor`
- [ ] `k_atr` between 1.5-2.5 (typical range)
- [ ] Estimated triggers ≥ 2 per day
- [ ] LKG file saved successfully
- [ ] JSON syntax valid (`python -m json.tool {symbol}_v7_2_lkg.json`)

---

## Advanced Usage

### Custom Calibration Logic

You can extend the `ParameterCalibrator` class for custom metrics:

```python
from tools.offline_calibrator import ParameterCalibrator

class CustomCalibrator(ParameterCalibrator):
    async def calibrate(self, days: int = 60):
        # Run base calibration
        params = await super().calibrate(days)

        # Add custom logic
        df5 = await self._fetch_historical_data(self.symbol, "5m", days)

        # Example: Adjust k_ATR based on Sharpe ratio
        returns = df5['close'].pct_change()
        sharpe = returns.mean() / returns.std()

        if sharpe > 0.5:
            params["k_atr"] *= 0.9  # Tighter stops for high Sharpe

        return params

# Usage
calibrator = CustomCalibrator("AAPL")
params = await calibrator.calibrate(days=90)
```

---

## File Outputs

### LKG File Structure

```json
{
  "windows_short": 7,
  "k_atr": 1.85,
  "regime_epsilon": 0.0048,
  "pause": {
    "theta_vix": 0.12,
    "theta_spread": 0.0007,
    "theta_atr": 2.1,
    "t_pause_min": 15
  },
  "A1_thr_sigma": 0.0045,
  "A1_rvol_lo": 0.82,
  "A1_rvol_hi": 1.75,
  "A1_time_exit_min": 45,
  "A2_grid_g": 0.85,
  "A2_grid_g_floor": 0.60,
  "A2_oneway_thr": 2.2,
  "A2_max_layers": 4,
  "B1_orb_len": 30,
  "B1_rvol_min": 0.85,
  "B1_pullback_window_M": 6,
  "B1_extend_orb": false
}
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `windows_short` | int | Short-term window for regime detection (days) |
| `k_atr` | float | ATR multiplier for stop-loss |
| `regime_epsilon` | float | SPY/QQQ regime threshold |
| `pause.*` | object | Market pause thresholds (VIX, spread, ATR) |
| `A1_thr_sigma` | float | VWAP deviation trigger threshold |
| `A1_rvol_lo/hi` | float | RVOL filter range |
| `A1_time_exit_min` | int | Forced exit time (minutes) |
| `A2_grid_g` | float | Grid layer spacing ($) |
| `A2_grid_g_floor` | float | Minimum grid spacing |
| `A2_oneway_thr` | float | One-way drawdown threshold |
| `A2_max_layers` | int | Max accumulation layers |
| `B1_orb_len` | int | Opening range length (minutes) |
| `B1_rvol_min` | float | Minimum RVOL for ORB trades |
| `B1_pullback_window_M` | int | Pullback detection window (5m bars) |
| `B1_extend_orb` | bool | Allow ORB extension beyond initial range |

---

## Performance Optimization

### Parallel Batch Calibration

For faster batch processing, modify `main()` to use `asyncio.gather()`:

```python
# In tools/offline_calibrator.py, replace sequential loop with:

tasks = []
for symbol in universe:
    if symbol in ["SPY", "VIX", "SQQQ"]:
        continue
    calibrator = ParameterCalibrator(symbol, template_symbol=args.template)
    tasks.append(calibrator.calibrate(days=args.days))

# Run all calibrations in parallel
results = await asyncio.gather(*tasks, return_exceptions=True)

# Save results
for symbol, result in zip(universe, results):
    if isinstance(result, dict) and result:
        calibrator = ParameterCalibrator(symbol)
        calibrator.save_results(result, output_dir=args.output_dir)
```

**Expected speedup:** 5-10x faster for 15+ symbols.

---

## Related Documentation

- **[SOP_ADDING_NEW_SYMBOLS.md](SOP_ADDING_NEW_SYMBOLS.md)** - Complete workflow for adding new symbols
- **[STRATEGY_DEPLOYMENT_SUMMARY.md](../strategies/configs/STRATEGY_DEPLOYMENT_SUMMARY.md)** - Parameter tables for all deployed strategies
- **[tsla_v7_2.py](../strategies/tsla_v7_2.py)** - Full strategy implementation with calibration logic (lines 668-817)

---

## FAQ

**Q: Can I use this tool for backtesting?**

A: Yes! Generate LKG files for different time periods:
```bash
# Q1 2024 parameters
python tools/offline_calibrator.py --symbol AAPL --days 60  # Uses last 60 days

# Archive with timestamp
mv aapl_v7_2_lkg.json backtest/aapl_v7_2_lkg_2024Q1.json
```

**Q: What if I want to override specific parameters?**

A: Manually edit the LKG file before copying to strategy config. The tool provides a starting point, not final values.

**Q: Why are some parameters not calibrated (e.g., `pause.theta_vix`)?**

A: Market-wide parameters (VIX threshold, spread threshold) are shared across all symbols and defined in the template. Only symbol-specific parameters are calibrated.

**Q: Can I run this in production before market open?**

A: Yes! Recommended workflow:
```bash
# 8:00 AM ET (before market open)
python tools/offline_calibrator.py --all --days 90

# Review generated LKG files
# Update strategy configs if parameters changed significantly
# Restart system to load new configs
```

---

## Change Log

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2024-10-08 | Initial release - extracted from tsla_v7_2.py |
| 1.0.1 | 2024-10-09 | Fixed pandas deprecation warning (`.fillna(method=)` → `.ffill()`) |

---

## Support

For issues or questions:
1. Check ClickHouse data availability: `SELECT COUNT(*) FROM trading.bars_1m_ibkr WHERE symbol='AAPL' AND d >= today()-60`
2. Verify template config exists: `ls -la strategies/configs/*_v7_2.json`
3. Review tool logs for specific error messages
4. See troubleshooting section above

**Tool Location:** `/workspace/Documents/trading/tools/offline_calibrator.py`
