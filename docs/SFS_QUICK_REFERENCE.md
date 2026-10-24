# SFS System Upgrade - Quick Reference Guide
**Last updated**: 2025-10-21

## What Changed?

### 1. New ClickHouse Schemas (Oct 20)

**New database**: `sfs` (separate namespace for Sliding Feature Service)

| Table | Purpose | Engine | TTL | Rows/day (19 symbols) |
|-------|---------|--------|-----|----------------------|
| `sfs.sfs_5s` | 1s→5s sliding bars | ReplacingMergeTree | 30 days | ~5,700 |
| `sfs.sfs_1m_tumble` | 1m tumbling aggregation | MergeTree | 365 days | ~7,410 |
| `trading.sfs_features` | Unified feature view | ReplacingMergeTree | 90 days | ~5,700 |

### 2. New Services (Oct 20–21)

| Service | Location | Purpose | Status |
|---------|----------|---------|--------|
| **SFS-1s** | `services/market/sfs_1s_service.py` | Kafka→Redis 1s bars | ✅ Complete, fixed envelope parsing |
| **SFS-5s** | `services/market/sfs_5s_service.py` | Redis 1s→CH 5s features | ⚠️ Schema ready, write logic stub |
| **SFS Facade** | `libs/dal/sfs_facade.py` | Strategy-friendly Redis reader | ✅ Ready |

### 3. Strategy Enablement (Oct 20–21)

**Before**: Only TSLA strategy
**After**: 6 parallel strategies now active

```yaml
strategies:
  - tsla_v7_2_001    (TSLA)    # Original
  - aapl_v7_2_001    (AAPL)    # NEW
  - nvda_v7_2_001    (NVDA)    # NEW
  - meta_v7_2_001    (META)    # NEW
  - msft_v7_2_001    (MSFT)    # NEW
  - amd_v7_2_001     (AMD)     # NEW
```

Each uses `TSLASimplifiedV7_2` with symbol-specific JSON config.

### 4. Critical Fix (Oct 20)

**Commit `03d74e7c`**: SFS-1s now handles both flat and envelope Kafka schemas
- **Before**: Could silently drop enveloped messages
- **After**: Safely extracts payload from `{metadata, data}` wrapper

### 5. IB Gateway Update (Oct 20)

- **Client ID**: `7` → `31` (new account)
- All strategies now connect via new gateway instance

---

## Architecture at a Glance

```
Kafka tick stream (md.tick.trade.v1)
          ↓
    SFS-1s Service
          ↓
    Redis (sfs:1s:SYMBOL)
    TTL=3600s, ring buffer
          ↓
    ┌─────────────────┐
    │ Strategies      │
    │ (6 parallel)    │
    └────────┬────────┘
             │
        ┌────┴──────────┐
        ↓               ↓
   Redis 1s reads   ClickHouse
   (sub-100ms)      CH features
   (100ms-1s)
```

---

## Key Data Formats

### Redis 1s Bar

```json
Key:   sfs:1s:AAPL
Type:  LIST (newest first, LPUSH)
Value: {
  "ts": 1729513845000,      // ms
  "open": 175.12,
  "high": 175.20,
  "low": 175.11,
  "close": 175.18,
  "volume": 2200,
  "trades": 5
}
TTL:   3600 seconds
```

### ClickHouse sfs_5s Row

```sql
symbol='AAPL'
ts=2025-10-21 10:30:45.000 UTC    -- 5s window end
open=175.10, high=175.20, low=175.10, close=175.18
volume=11000, trades=25
vwap=175.15, ret_5s=0.0046
ingest_ts=now()
```

---

## Files to Know

### Core Schemas
- `./schema/clickhouse/sfs_5s.sql`
- `./schema/clickhouse/sfs_1m_tumble.sql`
- `./schema/clickhouse/sfs_features.sql`

### Services
- `./services/market/sfs_1s_service.py`
- `./services/market/sfs_5s_service.py`
- `./libs/dal/sfs_facade.py`

### Strategies (Updated)
- `./strategies/reve_intraday_strategy.py` (SFS-aware)
- `./strategies/configs/amd_v7_2.json` (and 5 others)

### Configuration
- `./config.yml` (6 strategies enabled)
- `./scripts/vref_cache.json` (premarket cache)
- `./scripts/premarket.py` (V4)
- `./scripts/premarket_momentum_standalone.py` (V4a)

### Documentation
- `./doc/sfs_implementation_plan.md` (v2.2 design)

---

## Quick Commands

### Monitor SFS-1s Service

```bash
# Check if service is running
docker ps | grep sfs_1s

# View logs
docker logs -f <sfs_1s_container_id>

# Check Redis ring buffer size
redis-cli LLEN "sfs:1s:AAPL"  # per symbol

# View latest 1s bar for AAPL
redis-cli LINDEX "sfs:1s:AAPL" 0
```

### Query ClickHouse SFS Tables

```sql
-- Latest 5s bars for AAPL (last hour)
SELECT ts, open, high, low, close, volume, trades
FROM sfs.sfs_5s
WHERE symbol='AAPL' AND ts > now() - INTERVAL 1 HOUR
ORDER BY ts DESC
LIMIT 100;

-- 1m aggregates
SELECT ts, open, high, low, close, volume, vwap_1m
FROM sfs.sfs_1m_tumble
WHERE symbol='AAPL' AND ts > now() - INTERVAL 1 DAY
ORDER BY ts DESC;

-- Check sfs_features (merged view)
SELECT ts, close, volume, atr_1m_tumble, regime
FROM trading.sfs_features
WHERE symbol='AAPL' AND ts > now() - INTERVAL 10 MINUTE
ORDER BY ts DESC;
```

### Check Strategy Status

```bash
# View strategy logs (6 instances)
tail -f logs/trading_system.log | grep -i "reve\|tsla\|strategy"

# Check Prometheus SFS metrics
curl http://localhost:9090/api/v1/query?query=sfs1s_ring_len
```

### Premarket Cache

```bash
# View VREF cache
cat ~/.cache/premarket/vref_cache.json
# or custom location via $VREF_CACHE_PATH env var
```

---

## Status Dashboard

| Component | Status | Next Step |
|-----------|--------|-----------|
| SFS-1s (Redis) | ✅ Healthy | Monitor Redis memory |
| SFS-5s (ClickHouse) | ⚠️ Stub | Activate batch write logic |
| Strategies (6 active) | ✅ Running | Backtest hybrid features |
| Premarket V4/V4a | ✅ Running | Monitor fills/cleanup timing |
| Config sync | ✅ OK | Watch for symbol-specific errors |

---

## Known Issues & TODOs

1. **SFS-5s batch insert** (Line 136–140 in `sfs_5s_service.py`):
   ```python
   # TODO: Implement the actual batch insert logic using the ch_client
   # self.ch.insert(self.ch_table_name, feature_rows, ...)
   ```
   Currently logs but doesn't persist to ClickHouse.

2. **Feature derivation** (ATR, ORB, regime):
   - Not yet computed; placeholder 0.0/None values
   - Requires FeatureCalculator service

3. **ClickHouse schema version**:
   - `etl_ver` field ensures idempotent updates
   - Monitor ReplacingMergeTree merge activity

---

## Performance Reference

### Data Volume (Daily)

- **Redis**: 27.4 MB (19 symbols × 1.44 MB)
- **ClickHouse SFS**: 6.6 MB total
  - sfs_5s: 5.5 MB
  - sfs_1m_tumble: 0.8 MB
  - sfs_features: 0.3 MB

### Latency Budget

- Kafka tick → Redis 1s: **50–100 ms**
- Redis read (strategy): **5–20 ms**
- E2E tick-to-decision: **300–800 ms** (depends on strategy)

### Resource Footprint

- **Redis RAM**: ~1.5 MB/symbol
- **ClickHouse CPU**: Low (async batching)
- **Network**: <1% utilization

---

## Testing Checklist

- [ ] Unit test: Kafka envelope parsing (currently missing)
- [ ] Load test: All 19 symbols × 100 ticks/sec to Redis
- [ ] Integration: 6-strategy parallel order submission
- [ ] E2E: Premarket strategy with mock IBKR Gateway
- [ ] ClickHouse: Replication failover test

---

## Related Documentation

- **Full Analysis**: `SFS_SYSTEM_UPGRADE_ANALYSIS.md` (831 lines)
- **Design Document**: `doc/sfs_implementation_plan.md` (v2.2)
- **Strategy Guide**: `CLAUDE.md` (project rules & conventions)

---

**Questions?** Check the full analysis or reach out to:
- Infrastructure: SFS-1s service issues, Redis monitoring
- Strategy: Config/backtest questions, feature requests
- Operations: Runbooks, deployment procedures

