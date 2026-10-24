# Trading System Monitoring Guide

## Phase 6 - Monitoring & Alerts

This document describes the monitoring metrics and alert thresholds for the trading system data pipeline.

## Quick Start

```bash
# Run system health check
python3 scripts/monitor_system.py

# Check system logs
tail -f /tmp/trading_system.log

# Check Kafka consumer lag
kafka-consumer-groups --bootstrap-server localhost:9092 --describe --all-groups
```

## Monitoring Metrics

### 1. Tick Data Lake Health

**Metrics:**
- Total ticks collected (7-day window)
- Unique symbols coverage
- Hours of coverage
- Ticks per hour rate

**Thresholds:**
- ✅ HEALTHY: >100 ticks/hour during trading hours
- ⚠️  WARNING: 50-100 ticks/hour
- ❌ CRITICAL: <50 ticks/hour

**ClickHouse Query:**
```sql
SELECT
    COUNT(*) as total_ticks,
    COUNT(DISTINCT symbol) as unique_symbols,
    dateDiff('hour', MIN(ts), MAX(ts)) as hours_coverage,
    COUNT(*) / greatest(1, dateDiff('hour', MIN(ts), MAX(ts))) as ticks_per_hour
FROM lake.ticks_trades
WHERE ts > now() - INTERVAL 7 DAY
```

### 2. Spread & Quote Quality

**Metrics:**
- Average spread (bps)
- P95 spread (bps) per symbol
- Quote count (24h)

**Thresholds:**
- ✅ HEALTHY: Avg spread <5bps for liquid names
- ⚠️  WARNING: Avg spread 5-10bps
- ❌ CRITICAL: Avg spread >10bps or no quotes

**ClickHouse Query:**
```sql
SELECT
    symbol,
    COUNT(*) as quote_count,
    AVG((ask - bid) / ((bid + ask) / 2) * 10000) as avg_spread_bps,
    quantile(0.95)((ask - bid) / ((bid + ask) / 2) * 10000) as p95_spread_bps
FROM lake.quotes_l1
WHERE ts > now() - INTERVAL 1 DAY
  AND bid > 0 AND ask > 0
GROUP BY symbol
```

### 3. Bar Freshness

**Metrics:**
- Latest bar timestamp per resolution
- Age of latest bar (seconds)

**Thresholds:**
- ✅ HEALTHY: Age <5 minutes during trading hours
- ⚠️  WARNING: Age 5-60 minutes
- ❌ CRITICAL: Age >60 minutes

**ClickHouse Query:**
```sql
SELECT
    '5s' as resolution,
    MAX(ts_end) as latest_bar,
    dateDiff('second', MAX(ts_end), now()) as seconds_old
FROM trading.bars_5s
```

### 4. Gap Rate

**Metrics:**
- Expected vs actual bar count per symbol
- Gap rate (% missing bars)

**Thresholds:**
- ✅ HEALTHY: Gap rate <1%
- ⚠️  WARNING: Gap rate 1-5%
- ❌ CRITICAL: Gap rate >5%

**Action:**
- Check backfill service status
- Review IBKR connection logs
- Check for pacing violations

### 5. VIX Data Status

**Metrics:**
- VIX tick count (24h)
- Latest VIX timestamp
- Age of latest VIX (minutes)

**Thresholds:**
- ✅ HEALTHY: VIX data <5 minutes old during market hours
- ⚠️  WARNING: VIX data 5-60 minutes old
- ❌ CRITICAL: No VIX data or >60 minutes old

**Note:** VIX ingestor service must be running:
```bash
# Start VIX ingestor
python3 services/ibkr/vix_ingestor.py &
```

### 6. Feature Freshness

**Metrics:**
- Feature count per resolution (1h window)
- Latest feature timestamp
- Age of latest features (seconds)

**Thresholds:**
- ✅ HEALTHY: Features <5 minutes old during trading hours
- ⚠️  WARNING: Features 5-15 minutes old
- ❌ CRITICAL: No features or >15 minutes old

**Dependencies:**
- Bars must exist (features computed from bars)
- Feature calculator service must be running

### 7. Backfill Queue Status

**Kafka Consumer Groups to Monitor:**
- `backfill.5s.v1` - 5-second bar backfill
- `backfill.1m.v1` - 1-minute bar backfill
- `backfill.5m.v1` - 5-minute bar backfill
- `backfill.15m.v1` - 15-minute bar backfill

**Metrics:**
- Consumer lag (messages behind)
- Processing rate

**Thresholds:**
- ✅ HEALTHY: Lag <100 messages
- ⚠️  WARNING: Lag 100-1000 messages
- ❌ CRITICAL: Lag >1000 messages or growing

**Check Command:**
```bash
kafka-consumer-groups --bootstrap-server localhost:9092 --describe --group backfill.5s.v1
```

### 8. Writer Service Health

**Kafka Consumer Groups to Monitor:**
- `ch_writer_bars_5s` - 5s bars → ClickHouse
- `ch_writer_bars_1m_ibkr` - 1m bars → ClickHouse
- `ch_writer_bars_5m_ibkr` - 5m bars → ClickHouse
- `ch_writer_bars_15m_ibkr` - 15m bars → ClickHouse
- `lake_writer_trades` - Trades → lake.ticks_trades
- `lake_writer_quotes` - Quotes → lake.quotes_l1

**Metrics:**
- Consumer lag
- Insert success rate
- Insert errors (check logs)

**Thresholds:**
- ✅ HEALTHY: Lag <50, no errors
- ⚠️  WARNING: Lag 50-500, <1% error rate
- ❌ CRITICAL: Lag >500 or >1% error rate

### 9. IBKR Connection Health

**Log Patterns to Monitor:**
```bash
# Check for connection errors
grep -i "error\|disconnect\|timeout" /tmp/trading_system.log | tail -20

# Check for pacing violations
grep -i "pacing\|429" /tmp/trading_system.log | tail -20

# Check subscription status
grep -i "subscribed\|market data" /tmp/trading_system.log | tail -20
```

**Connection Indicators:**
- ✅ HEALTHY: "Connected to IB", "Subscribed to market data"
- ⚠️  WARNING: Reconnection attempts, timeouts
- ❌ CRITICAL: "clientId already in use", repeated failures

### 10. System Process Health

**Check Running Services:**
```bash
# Check if run_system.py is running
ps aux | grep run_system.py | grep -v grep

# Check process uptime
ps -o etime= -p $(pgrep -f run_system.py)
```

## Alert Configuration

### Critical Alerts (Page/SMS)

1. **No tick data for >10 minutes** (during trading hours)
2. **Bar freshness >60 minutes** (during trading hours)
3. **Gap rate >10%** for any symbol
4. **All symbols missing data**
5. **IBKR connection down >5 minutes**
6. **Backfill queue growing >1000 messages**

### Warning Alerts (Email/Slack)

1. **Tick rate <100/hour** (during trading hours)
2. **Spread >10bps** for liquid names
3. **VIX data >30 minutes old**
4. **Gap rate 5-10%** for any symbol
5. **Feature freshness >10 minutes**
6. **Writer lag >500 messages**

### Informational Alerts (Log only)

1. **Backfill requests generated**
2. **IBKR reconnections (successful)**
3. **Data quality issues (repaired)**
4. **Consumer rebalances**

## Grafana Dashboard Suggestions

### Panel 1: Tick Data Rate
- Line chart: Ticks/minute over last 24h
- Color thresholds: Green >100, Yellow 50-100, Red <50

### Panel 2: Spread Distribution
- Heatmap: Spread by symbol and time-of-day
- P50, P95, P99 percentiles

### Panel 3: Bar Coverage
- Table: Symbol | Resolution | Latest | Age | Gap%
- Status indicators (✅⚠️❌)

### Panel 4: Consumer Lag
- Line chart: Lag per consumer group
- Alert threshold line

### Panel 5: Feature Freshness
- Gauge: Age of latest features (5s, 1m, 5m)
- Color thresholds

### Panel 6: System Health Score
- Single stat: Composite health score (0-100)
- Calculation: Weighted average of all metrics

## Runbook

### Issue: No tick data incoming

1. Check if system is running: `ps aux | grep run_system.py`
2. Check IBKR connection: `grep "Connected to IB" /tmp/trading_system.log`
3. Check trading hours filter: Are we in trading hours? (4am-4pm ET)
4. Check symbol subscriptions: `grep "Subscribed" /tmp/trading_system.log`
5. Restart if needed: `killall python3 && python3 run_system.py`

### Issue: High gap rate

1. Check backfill service logs for errors
2. Check IBKR pacing violations
3. Check Kafka topic lag
4. Review gap time windows (pre-market vs RTH)
5. Manually trigger backfill if needed

### Issue: VIX data missing

1. Check if VIX ingestor is running
2. Verify CBOE index data subscription in IBKR
3. Check VIX table: `SELECT COUNT(*) FROM trading.refs_vix_ticks`
4. Start VIX ingestor: `python3 services/ibkr/vix_ingestor.py`

### Issue: Features not updating

1. Check if bars exist: `SELECT COUNT(*) FROM trading.bars_5s WHERE ts_start > now() - INTERVAL 1 HOUR`
2. Check feature calculator is running
3. Check feature calculator logs for errors
4. Verify waterline delay hasn't grown too large
5. Restart feature calculator if needed

## Automated Monitoring Script

The monitoring script is located at `scripts/monitor_system.py` and can be run on a schedule:

```bash
# Run every 5 minutes via cron
*/5 * * * * cd /workspace/Documents/trading && python3 scripts/monitor_system.py >> /var/log/trading_monitor.log 2>&1
```

The script generates a health report covering all metrics listed above.

## Conclusion

Monitoring is critical for maintaining data quality and ensuring strategies receive timely, accurate data. The metrics and thresholds defined here provide comprehensive coverage of the data pipeline from tick ingestion through feature calculation.

Regular monitoring (every 5 minutes during trading hours) combined with appropriate alerting ensures issues are detected and resolved quickly, minimizing impact on trading operations.
