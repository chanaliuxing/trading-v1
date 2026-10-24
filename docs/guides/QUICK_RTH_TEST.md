# RTH Test Suite - Quick Reference

## One-Line Execution

```bash
./test_rth_suite.sh
```

## Requirements

- ✅ Market hours: 9:30-16:00 ET, weekdays
- ✅ `run_system.py` running
- ✅ `TRADING_MODE=paper` in `.env`

## Expected Duration

**Total: ~20 minutes**

| Test | Time | What It Checks |
|------|------|----------------|
| 1. RFE Sink | 2-3 min | Active table growing with live features |
| 2. Fallback | 1-2 min | Supervisor detects stall & recovery |
| 3. Paper Mode | 15 min | Strategies run without errors |

## Success Looks Like

```
[INFO] ✅ ALL TESTS PASSED
```

## If Tests Fail

### Test 1 (RFE Sink) - No Growth

```bash
# Check RFE enabled
grep use_rfe_active_table config.yml

# Check strategy running
ps aux | grep enhanced_ml

# Manual query
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM trading.features_1m_active WHERE ts >= now() - interval 5 minute"
```

### Test 2 (Fallback) - Not Triggered

```bash
# Check supervisor running
grep "RFE fallback supervisor started" logs/run_system_*.log

# Wait longer (supervisor checks every 30s)
sleep 60
grep "RFE stall" logs/run_system_*.log
```

### Test 3 (Paper Mode) - No Decisions

```bash
# Check strategy enabled
grep -A 3 "enhanced_ml_001" config.yml | grep enabled

# Check for signals
grep "prediction\|signal" logs/trader.log | tail -20

# No decisions may be normal if:
# - Market quiet
# - No strong signals
# - Risk limits
```

## Manual Step-by-Step

If automated test fails, run manually:

### Test 1: RFE Sink

```bash
# Baseline
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM trading.features_1m_active WHERE ts >= now() - interval 10 minute"

# Wait 2 minutes
sleep 120

# Check again - should increase
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM trading.features_1m_active WHERE ts >= now() - interval 10 minute"
```

### Test 2: Fallback

```bash
TODAY=$(date +%Y-%m-%d)
HB="runs/ai_trader/$TODAY/rfe_heartbeat.json"

# Stale heartbeat
STALE=$(($(date +%s) - 60))
echo "{\"TSLA\":{\"ts_us\":${STALE}000000},\"AAPL\":{\"ts_us\":${STALE}000000}}" > "$HB"

# Wait & check
sleep 15
grep "RFE stall" logs/run_system_${TODAY}_*.log

# Recover
NOW=$(date +%s)
echo "{\"TSLA\":{\"ts_us\":${NOW}000000},\"AAPL\":{\"ts_us\":${NOW}000000}}" > "$HB"

# Wait & check
sleep 15
grep "RFE recovered" logs/run_system_${TODAY}_*.log
```

### Test 3: Paper Mode

```bash
# Check mode
grep TRADING_MODE .env

# Monitor 15 min
tail -f logs/trader.log | grep -E "decision|intent"

# Count after 15 min
grep -c "decision.*submit" logs/trader.log
```

## What Happens After All Tests Pass?

1. **Bulk backfill** historical data (operator action)
2. **Evening learning** runs automatically (16:20-22:00 ET)
3. **Switch to live** when ready (operator action)

## Emergency Stop

```bash
# Stop trading system
pkill -f run_system.py

# Stop all AI jobs
pkill -f ml_scheduler.py
pkill -f auto_loop.py
```

## Logs to Check

```bash
# Main system log
tail -f logs/run_system_$(date +%Y-%m-%d)_*.log

# Strategy decisions
tail -f logs/trader.log

# ML events
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_events.jsonl

# AI Brain actions
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ai_events.jsonl
```

## Contact

- **Logs**: `logs/` directory
- **Architecture**: `CLAUDE.md`, `RTH_TEST_GUIDE.md`
- **Issues**: Review detailed guide in `RTH_TEST_GUIDE.md`
