# RTH Test Suite - User Guide

## Overview

This guide covers the **Real-Time Hours (RTH)** test suite for validating the Gemini Brain's autonomous workflow. These tests **must be run during market hours** (9:30-16:00 ET, Monday-Friday).

## Quick Start

### Prerequisites

1. **Market hours**: 9:30-16:00 ET, weekdays only
2. **System running**: `run_system.py` must be active
3. **Paper mode**: Ensure `.env` has `TRADING_MODE=paper`
4. **Infrastructure**: ClickHouse, Kafka, Redis running

### Running the Full Suite

```bash
# One command to run all RTH tests
./test_rth_suite.sh
```

**Duration**: ~20 minutes total
- Test 1 (RFE sink): 2-3 minutes
- Test 2 (Fallback supervisor): 1-2 minutes
- Test 3 (Paper mode runtime): 15 minutes

---

## Test Breakdown

### Test 1: RFE + Active Sink Growth

**Purpose**: Verify that real-time features are flowing into `trading.features_1m_active`

**What it does**:
1. Queries baseline row count from `trading.features_1m_active` (last 10 min)
2. Waits 2 minutes
3. Queries again and compares counts
4. ✅ PASS if count increases (table is growing)

**Manual check** (if automated test fails):
```bash
# Check current count
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM trading.features_1m_active WHERE ts >= now() - interval 10 minute"

# Wait 2-3 minutes, run again - should increase
```

**Expected logs** (in `logs/run_system_*.log`):
```
feature_calc_strategy_guard    has_non_rfe=False
🔍 RFE fallback supervisor started
```

---

### Test 2: RFE Fallback Supervisor

**Purpose**: Verify the fallback mechanism kicks in when RFE heartbeat stalls

**What it does**:
1. Simulates stale heartbeat by writing old timestamps
2. Checks logs for "RFE stall detected" → fallback activation
3. Updates heartbeat to current time
4. Checks logs for "RFE recovered" → fallback stopped

**Manual execution**:

```bash
# 1. Simulate stale heartbeat
cd .
TODAY=$(date +%Y-%m-%d)
HB_FILE="runs/ai_trader/$TODAY/rfe_heartbeat.json"

# Write stale heartbeat (60s ago)
STALE_TS=$(($(date +%s) - 60))
cat > "$HB_FILE" <<EOF
{
  "TSLA": {"ts_us": ${STALE_TS}000000},
  "AAPL": {"ts_us": ${STALE_TS}000000},
  "NVDA": {"ts_us": ${STALE_TS}000000}
}
EOF

# 2. Wait 15 seconds, check logs
sleep 15
grep "RFE stall" logs/run_system_${TODAY}_*.log

# Expected: "🔻 RFE stall detected — starting 1m fallback"

# 3. Restore current heartbeat
NOW_TS=$(date +%s)
cat > "$HB_FILE" <<EOF
{
  "TSLA": {"ts_us": ${NOW_TS}000000},
  "AAPL": {"ts_us": ${NOW_TS}000000},
  "NVDA": {"ts_us": ${NOW_TS}000000}
}
EOF

# 4. Wait 15 seconds, check recovery
sleep 15
grep "RFE recovered" logs/run_system_${TODAY}_*.log

# Expected: "🔻 RFE recovered — stopped 1m fallback"
```

**Key logs to watch**:
- `logs/run_system_YYYY-MM-DD_*.log`
- Look for emoji indicators: 🔻 (stall), ✅ (recovery)

---

### Test 3: Paper Mode Runtime Sanity

**Purpose**: Ensure strategy runs normally for 15-30 min during RTH without errors

**What it does**:
1. Confirms `TRADING_MODE=paper` in `.env`
2. Monitors `logs/trader.log` for 15 minutes
3. Counts:
   - Decision submissions (`decision.*submit`, `order.*intent`)
   - Alignment guard skips (`ts_misaligned`, `alignment.*guard.*skip`)
4. ✅ PASS if:
   - Decisions are produced (may be 0 if no signals, that's OK)
   - Skip rate < 0.5%

**Manual monitoring**:

```bash
# Check .env
grep TRADING_MODE .env
# Should output: TRADING_MODE=paper

# Watch decisions flowing (real-time)
tail -f logs/trader.log | grep -E "decision|intent|submit"

# Count alignment skips over 15 min
# (Run this after 15 min of runtime)
grep -c "ts_misaligned\|alignment.*guard.*skip" logs/trader.log

# Count total decisions
grep -c "decision.*submit\|order.*intent" logs/trader.log

# Calculate skip rate
# Skip rate = (skips / decisions) * 100
# Should be < 0.5%
```

**What's normal**:
- **Low decision count**: Normal if market is quiet or no strong signals
- **Zero decisions**: Acceptable during low-volatility periods
- **Non-zero skip rate**: Some skips are expected (<0.5%)
- **High skip rate (>5%)**: Investigate alignment logic or RFE latency

---

## Expected Output

### Successful Run

```
==================================================
  RTH Test Suite for Gemini Brain Workflow
==================================================

⚠️  IMPORTANT: Run this during market hours (9:30-16:00 ET)

[INFO] Market hours check PASSED. Current ET time: 10:45

==========================================
Test 1: RFE + Active Sink Growth
==========================================
[INFO] Baseline count (last 10min): 450 rows
[INFO] Waiting 2 minutes for new data...
[INFO] After count (last 10min): 468 rows
[INFO] ✅ PASS: Active table growing (Δ=18 rows)

==========================================
Test 2: RFE Fallback Supervisor
==========================================
[INFO] Simulating stale heartbeat for RFE supervisor...
[INFO] ✅ PASS: RFE fallback supervisor detected stall and started fallback
[INFO] ✅ PASS: RFE recovery detected and fallback stopped

==========================================
Test 3: Paper Mode Runtime Sanity
==========================================
[INFO] TRADING_MODE=paper confirmed ✅
[INFO] Monitoring system for 15 minutes...
[INFO] [15min] Checking decision flow...
[INFO] Total decisions: 12
[INFO] Total alignment skips: 0
[INFO] ✅ PASS: Decisions are flowing
[INFO] Alignment guard skip rate: 0.00%
[INFO] ✅ PASS: Skip rate < 0.5%

==========================================
Test Suite Summary
==========================================
[INFO] ✅ ALL TESTS PASSED
```

---

## Troubleshooting

### "Outside market hours" error

**Cause**: Running outside 9:30-16:00 ET on weekdays

**Fix**: Run during market hours or override check (for dev):
```bash
# Comment out check_market_hours call in test_rth_suite.sh
# (Not recommended for production testing)
```

---

### Test 1 fails (no table growth)

**Possible causes**:
1. RFE not enabled in config
2. Strategy not running
3. No market activity

**Debug**:
```bash
# Check RFE config
grep -A 5 "use_rfe_active_table" config.yml
# Should be: use_rfe_active_table: true

# Check strategy status
grep "enhanced_ml_001" logs/trader.log | tail -20

# Check RFE sink logs
grep "features_1m_active" logs/run_system_*.log | tail -20

# Manually insert test row
docker exec trading_clickhouse clickhouse-client --query \
  "INSERT INTO trading.features_1m_active (symbol, ts, close) VALUES ('TEST', now(), 100.0)"

# Query to verify
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT * FROM trading.features_1m_active WHERE symbol='TEST' LIMIT 1"
```

---

### Test 2 fails (fallback not triggered)

**Possible causes**:
1. Fallback supervisor not running
2. Heartbeat file in wrong location
3. Timing issue (supervisor checks every 30s)

**Debug**:
```bash
# Check if supervisor started
grep "RFE fallback supervisor" logs/run_system_*.log

# Check heartbeat file location
ls -la runs/ai_trader/$(date +%Y-%m-%d)/rfe_heartbeat.json

# Check supervisor loop logs
grep "RFE.*checking" logs/run_system_*.log | tail -10

# Try longer wait (60s instead of 15s)
sleep 60
grep "RFE stall" logs/run_system_*.log
```

---

### Test 3 fails (no decisions)

**Possible causes**:
1. Strategy disabled
2. Market conditions (no signals)
3. Risk limits hit

**Debug**:
```bash
# Check strategy enabled
grep -A 5 "enhanced_ml_001" config.yml | grep enabled
# Should be: enabled: true

# Check shadow mode
grep -A 5 "enhanced_ml_001" config.yml | grep shadow_mode
# Should be: shadow_mode: false

# Check risk limits
grep -E "risk.*limit|dd_limit|max_concurrent" logs/trader.log | tail -10

# Check ML model predictions
grep "prediction\|signal\|confidence" logs/trader.log | tail -20

# Force a test decision (dry run)
# (Requires manual code trigger - consult team)
```

---

## Integration with Overall Test Plan

This RTH suite completes the **Gemini End-to-End Validation**:

✅ **Already tested** (non-RTH, completed):
1. Pre-flight (system startup)
2. OOF with active table override
3. 16:20 ML backfill
4. 16:30 labels recompute
5. 16:40 active DQ + auto re-backfill
6. Bulk backfill job
7. HPO auto-apply with gates

⏳ **This suite** (RTH-dependent):
8. RFE + Active sink growth ← **Test 1**
9. RFE fallback supervisor ← **Test 2**
10. Paper mode runtime sanity ← **Test 3**

---

## Next Steps After All Tests Pass

When **all 10 tests PASS**:

1. **Multi-year bulk backfill** (operator action):
   ```bash
   python - <<'PY'
   from scripts.ai_trader.ml_scheduler import enqueue_job
   jid = enqueue_job({
       "job_type": "bulk_backfill",
       "priority": 70,
       "params": {
           "symbols": ["TSLA", "AAPL", "NVDA"],
           "start_date": "2023-01-01",
           "end_date": "2025-11-06",
           "bar_size": "1m",
           "max_days_per_run": 30,
           "sleep_sec_between_days": 0.5
       }
   })
   print(f"Bulk backfill job: {jid}")
   PY
   ```

2. **Monitor overnight learning** (automatic at 16:20-22:00 ET):
   - Brain reviews OOF results
   - HPO runs if needed
   - Auto-apply if gates pass

3. **Switch to live trading** (operator action):
   ```bash
   # Update .env
   sed -i '' 's/TRADING_MODE=paper/TRADING_MODE=live/' .env

   # Restart system
   pkill -f run_system.py
   sleep 5
   python run_system.py &
   ```

4. **Production monitoring**:
   - Watch `runs/ai_trader/YYYY-MM-DD/ai_events.jsonl`
   - Review Brain decisions in `ml_review.json`
   - Monitor Prometheus/Grafana dashboards

---

## Success Criteria Summary

| Test | Metric | Threshold | Status |
|------|--------|-----------|--------|
| **RFE Sink** | Table growth | Δ > 0 rows/2min | ⏳ |
| **Fallback** | Supervisor logs | Stall + recovery detected | ⏳ |
| **Paper Mode** | Decision flow | Count > 0 (if signals) | ⏳ |
| **Paper Mode** | Skip rate | < 0.5% | ⏳ |

---

## Contact & Support

- **Issues**: Check `logs/trader.log`, `logs/run_system_*.log`
- **Architecture**: See `CLAUDE.md`, `SFS_QUICK_REFERENCE.md`
- **Slack**: #trading-alerts (for production issues)

---

**Last Updated**: 2025-11-07
**Version**: 1.0
**Author**: Trading Team + Claude
