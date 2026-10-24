# PnL Baseline & Performance Guard System

## Overview

This system provides **automated performance monitoring** and **risk reduction** based on PnL/Sharpe ratio comparisons against market benchmarks (SPY/QQQ).

**Components**:
1. **PnL Report Generator** - Daily aggregation of trading performance
2. **Performance Guard** - Automated risk adjustment when performance degrades
3. **Grafana Dashboard** - Visualization of PnL vs benchmarks

---

## Quick Start

### 1. Create ClickHouse Tables

```bash
docker exec trading_clickhouse clickhouse-client --multiquery < scripts/metrics/create_pnl_tables.sql
```

### 2. Populate Trading Data

**Option A: Use Real Trading Data (Production)**

If you have real trading data in `trading.pnl_daily` table from running the system:
```bash
# Verify you have data
docker exec trading_clickhouse clickhouse-client --query "SELECT COUNT(*) FROM trading.pnl_daily"

# If you have data, skip to step 3
```

**Option B: Generate Synthetic Data (Testing/Demo)**

If you're testing the system without real trading data:
```bash
# Generate 252 days (1 year) of synthetic PnL data with Sharpe 0.5
venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 252 --sharpe 0.5

# Or generate a losing strategy to test performance guard triggers
venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 60 --sharpe -0.2

# Or generate high-performing strategy
venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 100 --sharpe 1.2
```

Parameters for synthetic data:
- `--days`: Number of trading days (default: 252)
- `--sharpe`: Target Sharpe ratio (default: 0.5)
- `--capital`: Starting capital (default: 100000)
- `--vol`: Daily volatility (default: 0.015 = 1.5%)
- `--winrate`: Win rate (default: 0.55 = 55%)
- `--csv`: Optional CSV output path

### 3. Generate Initial Report

```bash
# Generate last 252 days, save to ClickHouse
venv/bin/python scripts/metrics/pnl_report.py --sink ch --days 252

# Or save to CSV only
venv/bin/python scripts/metrics/pnl_report.py --sink csv --days 60
```

### 4. Enable Performance Guard

Edit `config.yml`:
```yaml
risk_management:
  performance_guard:
    enabled: true          # Enable the guard
    dry_run: false         # Set to false to apply actual risk reductions
    min_sharpe: 0.3        # Trigger when Sharpe < 0.3
    min_ev: 0.0            # Trigger when EV < 0.0
    action: reduce         # 'reduce' or 'pause'
    reduce_factor: 0.5     # Reduce risk by 50%
```

### 5. Run System

```bash
venv/bin/python run_system.py
```

Performance Guard will check every 10 minutes and log actions to:
- Console logs
- `runs/ai_trader/performance_guard_state.json`
- ClickHouse `trading.performance_actions` table

---

## Components

### PnL Report Generator

**Script**: `scripts/metrics/pnl_report.py`

**What it does**:
- Fetches daily trading PnL from ClickHouse (or CSV)
- Fetches SPY benchmark returns
- Calculates Sharpe ratios over 20/60/252 day windows
- Computes edge (strategy Sharpe - benchmark Sharpe)
- Outputs to CSV and/or ClickHouse

**Usage**:
```bash
# Full options
venv/bin/python scripts/metrics/pnl_report.py \
  --days 252 \
  --benchmark SPY \
  --sink both \
  --output reports/pnl_baseline_daily.csv
```

**Output Columns**:
- `date` - Trading date
- `pnl` - Daily PnL
- `pnl_cum` - Cumulative PnL
- `ret` - Daily return (PnL / capital)
- `sharpe_20`, `sharpe_60`, `sharpe_252` - Rolling Sharpe ratios
- `benchmark_ret` - SPY daily return
- `benchmark_sharpe_*` - SPY Sharpe ratios
- `edge_sharpe_*` - Strategy - Benchmark Sharpe
- `wins`, `losses` - Daily win/loss counts

### Performance Guard

**Service**: `services/risk/performance_guard.py`

**What it does**:
- Monitors performance metrics (Sharpe, EV) over configurable window
- Triggers actions when metrics fall below thresholds
- Implements cool-off period to avoid thrashing
- Logs all actions for audit trail

**Actions**:
1. **reduce** - Writes `risk_reduction_active.json` signal file
   - Other services (SafetyKernel, HedgingService) can read this to scale down
   - `reduce_factor = 0.5` means 50% reduction in position sizes

2. **pause** - Writes `execution_paused.json` signal file
   - Signals to stop all trading

**Configuration** (`config.yml`):
```yaml
risk_management:
  performance_guard:
    enabled: true
    window_days: 20              # Look-back window
    min_sharpe: 0.3              # Minimum Sharpe threshold
    min_ev: 0.0                  # Minimum expectancy threshold
    action: reduce               # 'reduce' or 'pause'
    reduce_factor: 0.5           # Scale down factor (0.5 = 50%)
    cool_off_days: 3             # Wait period between actions
    dry_run: true                # true = log only, false = apply
    data_source: ch              # 'ch' or 'csv'
    csv_path: reports/pnl_baseline_daily.csv
    check_interval_sec: 600      # Check every 10 minutes
```

**Testing**:
```bash
# Run tests
venv/bin/pytest tests/services/risk/test_performance_guard.py -v

# Standalone test
venv/bin/python services/risk/performance_guard.py
```

---

## Integration

### run_system.py

Performance Guard is automatically initialized when `enabled: true` in config.

Startup logs:
```
✅ PerformanceGuard initialized (window=20d, min_sharpe=0.30, dry_run=True)
```

Runtime logs:
```
[INFO] performance_metrics_calculated sharpe=0.45 ev=125.0 window_days=20 current_state=normal
[INFO] performance_guard_check_passed reason="metrics_healthy" sharpe=0.45 ev=125.0
```

Action logs:
```
[WARNING] performance_guard_action_dry_run action="reduce" trigger="low_sharpe (0.25 < 0.3)" sharpe=0.25 ev=50.0 reduce_factor=0.5
```

### Reading Risk Reduction Signals

Other services can read the signal files:

```python
from pathlib import Path
import json

# Check if risk reduction is active
risk_file = Path("runs/ai_trader/risk_reduction_active.json")
if risk_file.exists():
    with open(risk_file) as f:
        risk_state = json.load(f)
        if risk_state.get("active"):
            reduce_factor = risk_state["reduce_factor"]
            # Scale down position sizes
            target_qty = target_qty * reduce_factor
```

---

## Grafana Dashboard

**File**: `dashboards/pnl_baseline_dashboard.json`

**Panels**:
1. Cumulative PnL vs SPY Benchmark (line chart)
2. Daily PnL (bar chart)
3. Sharpe Ratio Comparison (line chart with threshold)
4. Sharpe Edge (strategy - benchmark)
5. Win Rate (stat)
6. Latest Sharpe (stat with color thresholds)
7. Performance Actions Log (table)

**Import to Grafana**:
1. Open Grafana UI
2. Go to Dashboards > Import
3. Upload `dashboards/pnl_baseline_dashboard.json`
4. Configure ClickHouse datasource
5. Save

**Queries use**:
- Table: `trading.pnl_baseline`
- Table: `trading.performance_actions`

---

## Automation

### Daily Report Generation

Add to cron:
```bash
# Run daily at 5 PM ET after market close
0 17 * * * cd /path/to/trading && venv/bin/python scripts/metrics/pnl_report.py --sink ch --days 252 >> logs/pnl_report.log 2>&1
```

Or use system cron:
```bash
crontab -e
```

### LaunchAgent (macOS)

Create `~/Library/LaunchAgents/com.trading.pnl_report.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.trading.pnl_report</string>
    <key>ProgramArguments</key>
    <array>
        <string>./venv/bin/python</string>
        <string>./scripts/metrics/pnl_report.py</string>
        <string>--sink</string>
        <string>ch</string>
        <string>--days</string>
        <string>252</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>./logs/pnl_report.log</string>
    <key>StandardErrorPath</key>
    <string>./logs/pnl_report.err</string>
</dict>
</plist>
```

Load:
```bash
launchctl load ~/Library/LaunchAgents/com.trading.pnl_report.plist
```

---

## Monitoring

### Check Performance Guard State

```bash
cat runs/ai_trader/performance_guard_state.json
```

Output:
```json
{
  "state": "normal",
  "last_action_date": null,
  "updated_at": "2025-12-11T15:30:00+00:00"
}
```

### Query Actions History

```sql
SELECT
    ts,
    action_type,
    trigger,
    sharpe,
    threshold_sharpe,
    reduce_factor,
    previous_state,
    new_state
FROM trading.performance_actions
ORDER BY ts DESC
LIMIT 20;
```

### Check Risk Reduction Status

```bash
# Is risk reduction active?
cat runs/ai_trader/risk_reduction_active.json

# Is execution paused?
cat runs/ai_trader/execution_paused.json
```

---

## Troubleshooting

### No PnL data

**Problem**: Report shows "no_pnl_data_found"

**Solutions**:
1. Check if `trading.pnl_daily` table has data
2. If no data exists, you need to either:
   - **Option A**: Run the trading system to generate real data
   - **Option B**: Generate synthetic data for testing (see Quick Start step 2)
3. Verify date range with `--days` parameter
4. Check ClickHouse connection

```sql
-- Check if pnl_daily has data
docker exec trading_clickhouse clickhouse-client --query "SELECT count(*) FROM trading.pnl_daily"

-- Check date range
docker exec trading_clickhouse clickhouse-client --query "SELECT min(d_local), max(d_local) FROM trading.pnl_daily"

-- View sample data
docker exec trading_clickhouse clickhouse-client --query "SELECT * FROM trading.pnl_daily ORDER BY d_local DESC LIMIT 5 FORMAT Pretty"
```

**Generate test data if needed**:
```bash
# Generate 252 days of synthetic data
venv/bin/python scripts/metrics/generate_synthetic_pnl.py --days 252 --sharpe 0.5
```

### Performance Guard not triggering

**Problem**: Sharpe is low but no action taken

**Check**:
1. Is `enabled: true` in config?
2. Is `dry_run: true`? (will log but not apply)
3. Is cool-off period active? Check `last_action_date`
4. Are thresholds too low? Check `min_sharpe` value

```bash
# Check logs
tail -100 logs/run_system.log | grep performance_guard
```

### Grafana dashboard shows no data

**Problem**: Panels are empty

**Solutions**:
1. Verify ClickHouse datasource configured correctly
2. Check if `trading.pnl_baseline` table has data:
   ```sql
   SELECT count(*) FROM trading.pnl_baseline;
   ```
3. Verify query syntax in panel matches your ClickHouse version
4. Check Grafana logs for query errors

---

## Safety Notes

1. **Always test with `dry_run: true` first**
   - Verify logs show expected behavior
   - Check action triggers make sense

2. **Monitor cool-off period**
   - Default 3 days prevents thrashing
   - Adjust if needed for your trading frequency

3. **Benchmark selection**
   - Use SPY for broad market
   - Use QQQ for tech-heavy strategies
   - Ensure benchmark data availability in ClickHouse

4. **Risk reduction signal files**
   - Other services must be coded to read these files
   - Files persist across restarts
   - Manually delete if needed:
     ```bash
     rm runs/ai_trader/risk_reduction_active.json
     rm runs/ai_trader/execution_paused.json
     ```

---

## Future Enhancements

Possible improvements:
- [ ] Auto-resume when Sharpe recovers
- [ ] Email/Slack notifications on actions
- [ ] Multiple performance tiers (reduce 25%, 50%, pause)
- [ ] Integration with existing SafetyKernel parameters
- [ ] Drawdown-based triggers in addition to Sharpe
- [ ] Real-time dashboard updates via WebSocket

---

## Files Reference

- **Report Script**: `scripts/metrics/pnl_report.py`
- **Synthetic Data Generator**: `scripts/metrics/generate_synthetic_pnl.py`
- **Service**: `services/risk/performance_guard.py`
- **Config**: `config.yml` → `risk_management.performance_guard`
- **Tests**: `tests/services/risk/test_performance_guard.py`
- **SQL**: `scripts/metrics/create_pnl_tables.sql`
- **Dashboard**: `dashboards/pnl_baseline_dashboard.json`
- **Guide**: `dashboards/PNL_BASELINE_GUIDE.md`
- **State**: `runs/ai_trader/performance_guard_state.json`
- **Signals**: `runs/ai_trader/risk_reduction_active.json`, `execution_paused.json`

---

**Last Updated**: 2025-12-11
**Status**: ✅ Production Ready (with dry_run enabled by default)
