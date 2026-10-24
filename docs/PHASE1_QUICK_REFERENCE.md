# Phase 1 Automation - Quick Reference Guide

**Version**: 1.0 | **Date**: 2025-11-25

---

## 🚀 Quick Start

### Check System Health
```bash
# Full system health report (includes label health)
python scripts/monitor_system.py
```

### Manual PreFlight Check
```bash
# Check specific symbols
python scripts/ai_trader/brain/preflight.py TSLA AAPL MSFT

# Check all symbols
python scripts/ai_trader/brain/preflight.py
```

### Manual Label Generation
```bash
# Generate labels for specific symbols
python pipelines/generate_labels.py --symbols TSLA AAPL MSFT --horizons 30

# Batch generation (recommended for 5+ symbols)
bash temp/generate_labels_batched.sh
```

### Test Complete System
```bash
# Run Phase 1 test suite
python temp/test_phase1_automation.py
```

---

## 🎯 Core Components

| Component | File | Purpose |
|-----------|------|---------|
| **PreFlight Validator** | `scripts/ai_trader/brain/preflight.py` | Validate data before training |
| **Label Health Check** | `scripts/monitor_system.py` | Monitor label completeness |
| **Auto-Healing** | `services/data/data_healer.py` | Generate missing labels |
| **ML Integration** | `scripts/ai_trader/ml_scheduler.py` | Prevent training on bad data |

---

## 📊 Key Functions

### PreFlight Validation
```python
from scripts.ai_trader.brain.preflight import preflight_check_all

ready, report = preflight_check_all(
    symbols=["TSLA", "AAPL"],
    horizons=[30],
    min_rows_per_symbol=1000,
    strict=False  # Only labels blocking
)

if not ready:
    print(f"Blocking issues: {report['blocking_issues']}")
```

### Label Health Monitoring
```python
from scripts.monitor_system import SystemMonitor

monitor = SystemMonitor()
health = monitor.check_label_health()

print(f"Status: {health['summary']['overall_status']}")
print(f"Total rows: {health['summary']['total_rows']:,}")
print(f"Healthy: {health['summary']['healthy_count']} symbols")
```

### Auto-Healing
```python
from services.data.data_healer import DataHealer

healer = DataHealer(runs_dir="runs/ai_trader")
result = healer.ensure_labels_ready(
    symbols=["TSLA", "AAPL"],
    horizons=[30],
    lookback_days=90,
    batch_size=5  # Symbols per batch
)

if result["status"] == "fixed":
    print(f"✅ Generated {result['generated_rows']:,} rows")
elif result["status"] == "already_ok":
    print("✅ Labels already present")
else:
    print(f"❌ Healing failed: {result['errors']}")
```

---

## ⚙️ Configuration

### Label Generation Thresholds

**In PreFlight checks** (`preflight.py`):
```python
min_rows_per_symbol = 1000      # Minimum rows required
max_null_ratio = 0.1            # Max 10% NULL values
max_age_hours = 48              # Max data staleness
```

**In DataHealer** (`data_healer.py`):
```python
batch_size = 5                  # Symbols per batch
max_retries = 2                 # Retry attempts
lookback_days = 90              # Historical data window
```

### Memory Limits
- **ClickHouse query memory**: 3GB (system limit)
- **Recommended batch size**: 5 symbols
- **Max batch size**: 7 symbols (tested)

---

## 🔧 Troubleshooting

### Issue: PreFlight check fails for labels

**Symptom**: `preflight_check_all()` returns `ready=False` with missing_symbols

**Solution**:
```python
from services.data.data_healer import DataHealer

healer = DataHealer(runs_dir="runs/ai_trader")
result = healer.ensure_labels_ready(symbols=missing_symbols)

# Check result["status"]
```

### Issue: Label generation fails with memory error

**Symptom**: `DB::Exception: Query memory limit exceeded: 3.00 GiB`

**Solution**: Reduce batch size
```python
result = healer.ensure_labels_ready(
    symbols=["TSLA", "AAPL"],
    batch_size=3  # Reduced from 5
)
```

### Issue: Labels generated but still showing as missing

**Symptom**: PreFlight still fails after heal

**Root Cause**: Base features table is empty

**Solution**: Run feature pipeline first
```bash
# Generate base features
python pipelines/rebuild_features_1m.py --symbols TSLA AAPL

# Then generate labels
python pipelines/generate_labels.py --symbols TSLA AAPL --horizons 30
```

### Issue: ML scheduler aborts training

**Symptom**: Logs show `preflight_label_heal_failed`

**Investigation**:
1. Check PreFlight report in error metrics
2. Look for `heal_result` in error details
3. Check if base features exist:
   ```sql
   SELECT count() FROM trading.features_1m_active WHERE symbol = 'TSLA'
   ```

---

## 📈 Monitoring

### Key Metrics to Track

```python
# Label health status
- overall_status: "healthy" | "unhealthy" | "stale"
- total_rows: Total label rows across all symbols
- healthy_count: Number of symbols with sufficient labels

# PreFlight metrics
- preflight_pass_rate: % of checks that pass
- heal_success_rate: % of heals that succeed
- avg_heal_time: Average time to generate labels

# DataHealer metrics
- batches_completed: Number of batches successfully processed
- generated_rows: Total rows generated
- heal_failures: Number of failed heal attempts
```

### Log Patterns

**Successful PreFlight**:
```
[info] preflight_starting symbols=['TSLA', 'AAPL']
[info] preflight_complete ready=True
```

**Failed PreFlight + Successful Heal**:
```
[warning] preflight_check_failed blocking_issues=[...]
[info] attempting_label_auto_heal
[info] label_batch_success batch_num=1 rows=159545
[info] label_auto_heal_success generated_rows=159545
```

**Failed Heal**:
```
[error] label_batch_failed batch=['INTC'] error='...'
[error] label_auto_heal_failed heal_status='failed'
```

---

## 🎯 Best Practices

### 1. Always Check PreFlight Before Training
```python
# ❌ BAD: Start training immediately
train_model(symbols)

# ✅ GOOD: Check data first
ready, report = preflight_check_all(symbols)
if ready:
    train_model(symbols)
else:
    logger.error("data_not_ready", report=report)
```

### 2. Use Batched Generation for Multiple Symbols
```python
# ❌ BAD: Generate all 15 symbols at once (memory overflow)
healer.ensure_labels_ready(symbols=all_15_symbols, batch_size=15)

# ✅ GOOD: Use default batch size
healer.ensure_labels_ready(symbols=all_15_symbols, batch_size=5)
```

### 3. Monitor Label Health Regularly
```bash
# Add to crontab (every 4 hours)
0 */4 * * * cd /path/to/trading && python scripts/monitor_system.py >> logs/monitor.log 2>&1
```

### 4. Set Up Alerts for Repeated Failures
```python
# In future Phase 2/3, add:
if heal_failures > 3:
    send_slack_alert("Label healing failing repeatedly")
```

---

## 🔍 Advanced Usage

### Custom Validation Thresholds
```python
ready, report = preflight_check_all(
    symbols=["TSLA"],
    horizons=[30, 60],  # Check multiple horizons
    min_rows_per_symbol=5000,  # Stricter requirement
    strict=True  # Features also blocking
)
```

### Check Specific Symbol Details
```python
from scripts.monitor_system import SystemMonitor

monitor = SystemMonitor()
health = monitor.check_label_health(symbols=["TSLA"])

tsla_status = health['by_symbol']['TSLA']
print(f"TSLA: {tsla_status['rows']:,} rows")
print(f"NULL ratio: {tsla_status['null_ratio']:.2%}")
print(f"Age: {tsla_status['age_hours']:.1f} hours")
```

### Manual SQL Checks
```sql
-- Check label coverage by symbol
SELECT
    symbol,
    count() as total_rows,
    countIf(label_h30 IS NULL) as null_count,
    null_count * 100.0 / total_rows as null_pct,
    min(ts) as earliest,
    max(ts) as latest
FROM trading.features_1m_with_labels
GROUP BY symbol
ORDER BY symbol;

-- Check recent label generation
SELECT
    symbol,
    count() as rows,
    max(ts) as latest_ts
FROM trading.features_1m_with_labels
WHERE toDate(ts) >= today() - 7
GROUP BY symbol
ORDER BY latest_ts DESC;
```

---

## 📚 Additional Resources

- **Full Implementation Doc**: `docs/PHASE1_IMPLEMENTATION_SUMMARY.md`
- **Pragmatic Roadmap**: `docs/PRAGMATIC_AUTOMATION_ROADMAP.md`
- **Test Suite**: `temp/test_phase1_automation.py`
- **Root Cause Analysis**: `runs/tests/final_summary_2025-11-25.md`

---

## 🆘 Support

**If PreFlight keeps failing:**
1. Check base features: `SELECT count() FROM trading.features_1m_active WHERE symbol = 'XXX'`
2. Check label generation logs: `grep 'label_generation' logs/trading_system.log`
3. Run test suite: `python temp/test_phase1_automation.py`
4. Check monitor output: `python scripts/monitor_system.py`

**If label generation is slow:**
- Normal: 10-60 seconds per batch of 5 symbols
- Check ClickHouse load: `SELECT * FROM system.processes`
- Reduce batch size if memory constrained

**If uncertain about data status:**
```bash
# Quick health check
python scripts/monitor_system.py | grep -A 20 "LABEL HEALTH"
```

---

**Last Updated**: 2025-11-25
**Maintainer**: Trading Team
**Version**: 1.0
