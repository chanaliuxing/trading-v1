# SOP: Adding New Symbols to the Trading System

**Date:** 2025-10-08
**Status:** ✅ ACTIVE
**Purpose:** Standard Operating Procedure for adding new symbols to the universe

---

## Executive Summary

The trading system uses a **central universe list** in `config.yml` as the single source of truth for symbol management. Adding a new symbol requires **2 simple steps** and triggers automatic data subscription and backfill.

---

## Current Universe-Based Architecture

### ✅ Services Using Universe List

| Service | File | Status | Evidence |
|---------|------|--------|----------|
| **MarketDataService** | `run_system.py:104` | ✅ FULL | `symbols = config.universe` |
| **OfflineDataReconciler** | `offline_data_reconciler.py:114-119` | ✅ FULL | `universe = getattr(config, 'universe', None)` |
| **CatchupService** | `catchup_service.py:172-185` | ✅ FULL | `universe = getattr(config, 'universe', None)` |

**All services now prioritize the central `config.universe` list as the single source of truth.**

---

## How to Add a New Symbol (2 Steps)

### Step 1: Add Symbol to Universe List

Edit `/workspace/Documents/trading/config.yml`:

```yaml
universe:
- AAPL
- TSLA
- NVDA
- SPY
- VIX
- MSFT
- AMD
- META    # ← Existing symbol
- AMZN
- GOOG    # ← ADD YOUR NEW SYMBOL HERE
```

**Location:** Lines 167-185

### Step 2: Restart the System

```bash
make restart
```

That's it! The system will automatically:
1. ✅ Subscribe to real-time market data for the new symbol (via `MarketDataService`)
2. ✅ Backfill historical data overnight (via `OfflineDataReconciler`)
3. ✅ Detect and repair gaps in real-time (via `CatchupService` reading from universe)

---

## What Happens After Adding a Symbol

### Immediate (Real-Time Subscription)
When you restart the system:
- `run_system.py` reads `config.universe` (line 104)
- Passes symbols to `MarketDataService` (line 136)
- `IBMarketDataAdapter` subscribes to IBKR real-time feed
- 5-second bars start flowing to Kafka → ClickHouse

### Overnight (Historical Backfill)
When `OfflineDataReconciler` runs (recommended after 2:00 AM ET):
- Reads `config.universe` (lines 114-119)
- Queries ClickHouse for missing bars (RTH hours only)
- Sends backfill requests to Kafka
- `BackfillService` fetches from IBKR → writes to ClickHouse

### Continuous (Gap Detection)
`CatchupService` runs every 2 minutes:
- Reads symbols from `config.universe` (lines 172-185)
- Detects gaps in real-time data
- Sends small backfill requests (<60 minutes)

---

## For Strategy Deployment

After adding a symbol to the universe, you can create a strategy config:

### Example: Adding AAPL Strategy

1. **Create Strategy Config**
   File: `/workspace/Documents/trading/strategies/configs/aapl_v7_2.json`

   ```json
   {
     "symbol": "AAPL",
     "asof": "2025-10-08",
     "description": "AAPL Intraday Long V7.2",

     "premarket_calibration": {
       "enabled": true,
       "use_config_values": true  // ← Use JSON values, don't recalculate
     },

     "A1": {
       "thr_sigma": 0.0045,  // AAPL-specific value (from doc/strategies/aapl.md)
       "rvol_lo": 0.85,
       "rvol_hi": 1.80
     },

     // ... rest of config from tsla_v7_2.json template
   }
   ```

2. **Strategy is Ready**
   No code changes needed! The strategy engine will load the config automatically.

---

## Data Pipeline Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ config.yml                                                       │
│ ┌─────────────────┐                                             │
│ │ universe:       │                                             │
│ │ - AAPL          │                                             │
│ │ - TSLA          │                                             │
│ │ - GOOG  ← NEW!  │                                             │
│ └─────────────────┘                                             │
└───────────────┬─────────────────────────────────────────────────┘
                │
    ┌───────────┴───────────┬──────────────────────────┐
    │                       │                          │
    ▼                       ▼                          ▼
┌──────────────┐   ┌─────────────────┐   ┌─────────────────────┐
│ run_system.py│   │ Offline         │   │ CatchupService      │
│              │   │ DataReconciler  │   │                     │
│ Line 104:    │   │                 │   │ Line 182:           │
│ symbols =    │   │ Line 116:       │   │ universe =          │
│ config.      │   │ universe =      │   │ config.universe     │
│ universe     │   │ config.universe │   │                     │
└──────┬───────┘   └────────┬────────┘   └──────────┬──────────┘
       │                    │                       │
       │                    │                       │
       ▼                    ▼                       ▼
┌─────────────────┐ ┌──────────────┐ ┌──────────────────────┐
│ MarketData      │ │ Nightly      │ │ Intraday Gap         │
│ Service         │ │ Backfill     │ │ Detection            │
│                 │ │              │ │                      │
│ IBKR Real-time  │ │ IBKR Hist    │ │ Small Backfills      │
│ → Kafka → CH    │ │ → Kafka → CH │ │ → Kafka → CH         │
└─────────────────┘ └──────────────┘ └──────────────────────┘
```

---

## Verification Commands

### Check if Symbol is in Universe
```bash
grep -A 20 "universe:" config.yml | grep "GOOG"
```

### Check Real-Time Data Flow
```bash
# Query ClickHouse for today's data
clickhouse-client --query "
SELECT symbol, count(*) as bars, min(ts_start), max(ts_start)
FROM trading.bars_5s
WHERE symbol = 'GOOG' AND d = today()
GROUP BY symbol
"
```

### Check Backfill Data
```bash
clickhouse-client --query "
SELECT symbol, count(*) as bars, min(ts_start), max(ts_start)
FROM trading.bars_1m_ibkr
WHERE symbol = 'GOOG' AND d >= today() - 3
GROUP BY symbol
"
```

---

## Troubleshooting

### Symbol Not Receiving Data

**Check 1:** Verify symbol in universe
```bash
grep "GOOG" config.yml
```

**Check 2:** Verify system restarted
```bash
ps aux | grep run_system.py
# Should show new PID after restart
```

**Check 3:** Check IBKR subscription
```bash
# Look for subscription confirmation in logs
tail -f logs/market_data.log | grep "Subscribed to market data"
```

**Check 4:** Check Kafka topic
```bash
kafka-console-consumer --bootstrap-server localhost:9092 \
  --topic md.bar.5s.v2 \
  --from-beginning | grep "GOOG"
```

### Backfill Not Running

**Check 1:** Run manual reconciliation
```bash
make recon-all
```

**Check 2:** Check reconciliation logs
```bash
tail -f logs/reconciliation.log
```

---

## Summary

**Current State:**
- ✅ MarketDataService: Uses `config.universe` for real-time subscriptions
- ✅ OfflineDataReconciler: Uses `config.universe` for nightly backfill
- ✅ CatchupService: Uses `config.universe` for gap detection (updated 2025-10-08)

**Adding a Symbol = 2 Steps:**
1. Add to `config.yml` universe list
2. Run `make restart`

**Everything Else is Automatic:**
- Real-time data subscription ✅
- Historical data backfill ✅
- Gap detection and repair ✅
- Strategy config creation (manual, as needed)

**"即插即用" Status:** 100% COMPLETE! All data services now read from the central `config.universe` list.
