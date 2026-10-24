# Phase 0 Dashboard Troubleshooting Guide

**Created**: 2025-10-14

---

## 🔍 Understanding "No Data" Errors

When you see **"No data"** in dashboard panels, it's usually **NOT an error** - it means there's simply no data matching that query in the time range.

### Common Scenarios

#### ✅ System Running But Not Trading

**What you'll see:**
- ✅ System Status: 🟢 RUNNING
- 🟡 Trading Status: IDLE (No Trading)
- ✅ ClickHouse events in logs
- ⚠️ No order fills/rejects/skips

**What it means:**
- Your system is alive and healthy
- Services are running (ClickHouse, Kafka, etc.)
- But no trading signals are being generated

**Why this happens:**
1. **Market is closed** (outside trading hours)
2. **No strategy signals** (market conditions don't match entry criteria)
3. **Backtesting/warm-up mode** (not in live trading mode)
4. **All orders blocked** by risk controls

**What to check:**
```bash
# Check if system is running
ps aux | grep python | grep run_system

# Check recent logs for strategy activity
tail -100 /workspace/grafana-stack/logs/trader.log | grep -i "strategy\|signal\|entry"

# Check market hours
date
```

---

#### ⚠️ IBKR Connection Issues

**What you'll see:**
- 🟢 System Status: RUNNING
- 🔴 Market Data Status: Data Issues
- ⚠️ Error: "IBKR returned no historical data"
- ⚠️ Error: "API connection failed: TimeoutError()"

**What it means:**
- Your system can't fetch data from Interactive Brokers

**Why this happens:**
1. **IBKR Gateway/TWS not running**
2. **Market is closed** (IBKR doesn't provide real-time data outside market hours)
3. **API connection timed out**
4. **Requesting data for unavailable symbols**

**How to fix:**
```bash
# Check if IBKR Gateway is running
ps aux | grep -i "ibgateway\|tws"

# Start IBKR Gateway (if not running)
# See your IBKR setup documentation

# Check if requesting valid symbols
grep -A 5 "backfill:" config.yml
```

---

#### ⚠️ Kafka Heartbeat Issues

**What you'll see:**
- ⚠️ Error: "Heartbeat session expired - marking coordinator dead"
- ⚠️ Error: "Heartbeat failed: local member_id was not recognized"

**What it means:**
- Kafka consumer group coordination issues
- Usually happens during startup or when reconnecting

**Why this happens:**
1. **Kafka restarting**
2. **Network hiccup**
3. **Consumer taking too long to process messages**

**Impact:**
- Minor - Kafka will automatically reconnect
- May see brief gaps in data streaming

**How to fix (if persistent):**
```bash
# Check if Kafka is running
ps aux | grep kafka

# Check Kafka logs
tail -100 /path/to/kafka/logs/server.log

# Restart system if persistent
pkill -f run_system.py
python run_system.py
```

---

## 📊 Dashboard Panel Meanings

### System Health Section

| Panel | What It Shows | Green | Yellow | Red |
|-------|---------------|-------|--------|-----|
| System Status | Is run_system.py active? | RUNNING | - | STOPPED |
| Active Services | Count of logging services | >10 | 5-10 | <5 |
| Error Rate | Errors in last 5m | 0 | 1-10 | >10 |
| IBKR Connection | Can fetch market data? | CONNECTED | - | DISCONNECTED |
| ClickHouse Status | Database working? | UP | - | DOWN |

### Trading Activity Section

| Panel | What It Shows | No Data Means |
|-------|---------------|---------------|
| Order Fills | Executed orders | No trades executed |
| Order Rejects | Rejected orders | All orders passed or no orders |
| Overlay Blocks | Risk blocks | No risk violations |
| Order Skips | Skipped opportunities | No signals or all taken |

### Current System Issues Section

| Panel | What It Shows | Example Values |
|-------|---------------|----------------|
| Active Issues | Top problems in last 10m | "IBKR returned no historical data": 22 |
| Trading Status | Are we trading? | 🟡 IDLE / 🟢 ACTIVE |
| Market Data Status | Data flowing? | 🔴 Data Issues / 🟢 Data Flowing |

---

## 🎯 Quick Diagnostics

### "Why am I not trading?"

Run this checklist:

1. **Check Trading Status panel**
   - If 🟡 IDLE → Look at "Active Issues" table

2. **Look at Active Issues (last 10m)**
   - See "IBKR returned no historical data" → IBKR issue
   - See "Heartbeat failed" → Kafka issue (usually auto-recovers)
   - See "API connection failed" → IBKR Gateway not running

3. **Check Live Logs panel**
   - Filter by: `level="warning" or level="error"`
   - Look for recent problems

4. **Check system time vs market hours**
   ```bash
   date
   # Market hours: 9:30 AM - 4:00 PM ET on weekdays
   ```

5. **Check strategy logs**
   ```bash
   tail -200 /workspace/grafana-stack/logs/trader.log | \
     jq -r 'select(.event | contains("signal") or contains("entry") or contains("gate")) | {time:.timestamp, event:.event, reason:.reason}'
   ```

---

## 🧪 Testing the Dashboard

### Test 1: Verify System is Running

**Expected:**
- System Status: 🟢 RUNNING
- Active Services: > 5
- Live Logs: Scrolling with new entries

**If failed:**
```bash
# Start system
cd /workspace/Documents/trading
python run_system.py
```

### Test 2: Check Data Flow

**Expected:**
- Service Activity chart: Lines showing activity
- Service Event Counts: Table with events

**If empty:**
- Check time range (top-right) - try "Last 6 hours"
- Verify logs exist:
  ```bash
  ls -lh /workspace/grafana-stack/logs/trader.log
  tail -10 /workspace/grafana-stack/logs/trader.log
  ```

### Test 3: Understand Trading State

**During market hours (9:30 AM - 4:00 PM ET):**
- Trading Status should eventually show 🟢 ACTIVE (if strategies trigger)
- Market Data Status should be 🟢 Data Flowing

**Outside market hours:**
- 🟡 IDLE is normal
- 🔴 Data Issues is normal (IBKR doesn't provide data)

---

## 💡 Pro Tips

### 1. Use Time Range Selector

If panels show "No data":
- Try different time ranges: Last 1h, Last 6h, Last 24h
- Trading only happens during market hours!

### 2. Watch the Live Logs

The bottom "Live Logs" panel is your friend:
- Shows real-time what's happening
- Use the search box to filter
- Click on a log entry to see full details

### 3. "Active Issues" Table

This is the most useful panel for diagnosing problems:
- Shows what's actually going wrong RIGHT NOW
- Updates every 10 seconds
- Empty = no issues!

### 4. Export Data for Analysis

From any panel:
- Click panel title → Inspect → Data
- See the raw query results
- Copy query to test in Loki directly

---

## 📝 Expected Behavior by Time

| Time | Expected State | Panels with Data | Panels with "No Data" |
|------|----------------|------------------|----------------------|
| **Market Open** (9:30-16:00 ET) | Trading | System Health, Trading Activity, Service Details | (none) |
| **Pre-Market** (4:00-9:30 ET) | Warming Up | System Health, Service Details | Trading Activity |
| **After Hours** (16:00-20:00 ET) | Idle | System Health, Service Details | Trading Activity |
| **Market Closed** (Weekends) | Idle/Stopped | System Health (if running) | Trading Activity |

---

## 🚨 When to Worry

### ⚠️ Yellow Flags (Check but not urgent)

- Trading Status: 🟡 IDLE during market hours
- Market Data Status: 🔴 Data Issues during market hours
- Active Issues: <10 errors in 10m

**Action:** Check logs, verify IBKR connection

### 🔴 Red Flags (Fix immediately)

- System Status: 🔴 STOPPED (when it should be running)
- Error Rate: >50 in 5m
- Active Issues: >50 errors in 10m
- No logs in Live Logs panel for >1 minute

**Action:** System may be crashed or hung
```bash
# Check if process is alive
ps aux | grep run_system

# Check for errors
tail -100 /workspace/grafana-stack/logs/trader.log | grep ERROR

# Restart if needed
pkill -f run_system.py
python run_system.py
```

---

## 📚 Related Docs

- `DASHBOARD_FIXES_SUMMARY.md` - What was fixed and why
- `DASHBOARD_IMPLEMENTATION_PLAN.md` - Future enhancements
- `../MONITORING.md` - Monitoring architecture

---

## 🆘 Still Stuck?

1. **Check Grafana datasource:**
   - Grafana → Configuration → Data sources → Loki
   - Test connection should succeed

2. **Check Loki is running:**
   ```bash
   ps aux | grep loki
   curl http://localhost:3100/ready
   ```

3. **Check Promtail is collecting:**
   ```bash
   ps aux | grep promtail
   cat /workspace/grafana-stack/promtail/positions.yaml
   ```

4. **Check log file exists:**
   ```bash
   ls -lh /workspace/grafana-stack/logs/trader.log
   ```

---

**Last Updated**: 2025-10-14
**Dashboard Version**: 2.0 (with Current Issues section)
