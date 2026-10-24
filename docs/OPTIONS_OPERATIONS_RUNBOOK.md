# Options Hedging Operations Runbook

**Date**: 2025-11-02
**Target Audience**: Trading Operations Team
**Status**: ✅ Complete

---

## Table of Contents

1. [Daily Operations](#daily-operations)
2. [Manual Intervention Scripts](#manual-intervention-scripts)
3. [Monitoring & Alerts](#monitoring--alerts)
4. [Troubleshooting Guide](#troubleshooting-guide)
5. [Emergency Procedures](#emergency-procedures)
6. [Maintenance Tasks](#maintenance-tasks)

---

## Daily Operations

### Morning Pre-Market (8:00 AM - 9:30 AM ET)

**1. Check System Health**
```bash
# Verify IBKR Gateway is running
ps aux | grep -i gateway

# Check position metrics
python scripts/ai_trader/risk/manage_positions.py --summary

# Review yesterday's hedges
cat runs/ai_trader/$(date -d yesterday +%Y-%m-%d)/hedging_events.jsonl | \
  jq 'select(.event_type=="execution")'
```

**2. Check Expiring Positions**
```bash
# Positions expiring within 7 days
python scripts/ai_trader/risk/manage_positions.py --expiring --days 7
```

**Action**: If positions are expiring, decide:
- Roll to next expiry (manual)
- Close position (submit closing order)
- Let expire (if OTM and worthless)

**3. Verify Daily Limits Reset**
```bash
# Check Grafana dashboard: Options Hedging Monitor
# Verify: ai_hedging_daily_contracts = 0
# Verify: ai_hedging_daily_notional_usd = 0
```

**Action**: If limits didn't reset, restart metrics updater:
```bash
pkill -f update_hedging_metrics
python scripts/ai_trader/risk/update_hedging_metrics.py --loop &
```

---

### Intraday Monitoring (9:30 AM - 4:00 PM ET)

**1. Watch for Hedge Triggers**
```bash
# Monitor events in real-time
tail -f runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl | \
  jq 'select(.event_type | IN("selection", "execution", "rejection"))'
```

**2. Check Grafana Alerts**
- Open: `http://grafana:3000/d/options-hedging-monitor`
- Watch for:
  - 🔴 Daily limit approaching (>45 contracts or >$90k)
  - 🟡 Wide spreads (>50 bps average)
  - 🟡 Low fill rate (<70%)
  - 🟡 High rejection rate

**3. Verify Order Fills**
```sql
-- Check orders submitted in last hour
SELECT
  symbol,
  side,
  qty,
  limit_price,
  state,
  created_at
FROM core.core_order
WHERE strategy_id = 'hedge_overlay'
  AND order_type = 'OPT_LMT'
  AND created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC;
```

**Action**: If order stuck in SUBMITTED for >10 min:
```bash
# Check IBKR TWS for rejection reasons
# Consider canceling and re-submitting
python scripts/ai_trader/risk/cancel_hedge_orders.py --order-id <ID>
```

---

### End of Day (4:00 PM - 5:00 PM ET)

**1. Review Daily Stats**
```bash
python -c "
from scripts.ai_trader.risk.hedging_events import HedgingEventsLogger
logger = HedgingEventsLogger('runs/ai_trader')
stats = logger.get_daily_stats()
import json
print(json.dumps(stats, indent=2))
"
```

**Expected Output**:
```json
{
  "total_events": 15,
  "selections": {"total": 3, "success": 2, "failure": 1},
  "executions": {"total": 2, "success": 2, "failure": 0},
  "rejections": {"daily_limit": 1},
  "avg_spread_bps": 18.5,
  "avg_oi": 12500
}
```

**2. Update Position P&L**
```bash
python scripts/ai_trader/risk/manage_positions.py --update-data --summary
```

**3. Generate Daily Report**
```bash
# TODO: Automated daily report script
# For now, manually check:
# - Number of hedges executed
# - Fill rate
# - Average slippage
# - Position Greeks evolution
# - Unrealized P&L
```

---

## Manual Intervention Scripts

### 1. Manual Hedge Trigger

**Use Case**: Manually trigger hedge outside normal kill-switch

**Script**: `scripts/ai_trader/risk/manual_hedge_trigger.py`

**Examples**:

```bash
# Dry-run selection only (safe)
python scripts/ai_trader/risk/manual_hedge_trigger.py --dry-run

# Execute small hedge for vol spike
python scripts/ai_trader/risk/manual_hedge_trigger.py \
  --intent-type vol_spike \
  --size small \
  --execute

# Execute large SPY put for drawdown protection
python scripts/ai_trader/risk/manual_hedge_trigger.py \
  --intent-type drawdown_protect \
  --instrument SPY \
  --size large \
  --urgency high \
  --execute
```

**Safety**:
- Requires `--execute` flag
- Prompts for confirmation
- Respects daily limits (cannot bypass)
- Logs to `hedging_events.jsonl`

---

### 2. Cancel Hedge Orders

**Use Case**: Cancel stale or mis-priced limit orders

**Script**: `scripts/ai_trader/risk/cancel_hedge_orders.py`

**Examples**:

```bash
# List pending orders
python scripts/ai_trader/risk/cancel_hedge_orders.py --list

# Cancel specific order
python scripts/ai_trader/risk/cancel_hedge_orders.py --order-id 12345

# Cancel ALL pending orders (DANGEROUS)
python scripts/ai_trader/risk/cancel_hedge_orders.py --all --confirm
```

**When to Use**:
- Order stuck in SUBMITTED for >30 min
- Limit price no longer valid (market moved)
- End of day cleanup (avoid overnight exposure)

---

### 3. Manage Positions

**Use Case**: View, monitor, and close option positions

**Script**: `scripts/ai_trader/risk/manage_positions.py`

**Examples**:

```bash
# Show portfolio summary
python scripts/ai_trader/risk/manage_positions.py --summary

# List all positions
python scripts/ai_trader/risk/manage_positions.py --list

# Check expiring positions (DTE ≤ 7)
python scripts/ai_trader/risk/manage_positions.py --expiring --days 7

# Update market data from IBKR
python scripts/ai_trader/risk/manage_positions.py --update-data

# View specific position detail
python scripts/ai_trader/risk/manage_positions.py \
  --position "SPY   251116P00450000"

# Close position (NOT IMPLEMENTED - manual TWS required)
python scripts/ai_trader/risk/manage_positions.py \
  --close "SPY   251116P00450000" \
  --confirm
```

---

### 4. Update Metrics

**Use Case**: Manually trigger metrics update

**Script**: `scripts/ai_trader/risk/update_hedging_metrics.py`

**Examples**:

```bash
# Single run (update once)
python scripts/ai_trader/risk/update_hedging_metrics.py

# Continuous mode (every 5 min)
python scripts/ai_trader/risk/update_hedging_metrics.py --loop
```

**When to Use**:
- Metrics updater crashed
- Position metrics stale in Grafana
- After manual position changes

---

## Monitoring & Alerts

### Grafana Dashboard

**URL**: `http://grafana:3000/d/options-hedging-monitor`

**Panels**:
1. **Selection Performance**: Success rate, latency, rate
2. **Contract Quality**: Spread, OI, delta error
3. **Execution Status**: Rate by status, rejection reasons
4. **Daily Limits**: Contracts, notional, utilization gauges
5. **Position Risk**: Open positions, Greeks, P&L, expiring

**Refresh**: 30 seconds (configurable)

---

### Alert Rules

**Critical Alerts** (PagerDuty/Slack):

1. **Daily Limit Exceeded**
   - Trigger: `ai_hedging_daily_contracts >= 50`
   - Action: Manual review required, system blocks new hedges

2. **Execution Failures**
   - Trigger: `rate(execution_total{status="error"}[5m]) > 0.5`
   - Action: Check IBKR connection, review logs

3. **Positions Expiring**
   - Trigger: `ai_hedging_positions_expiring_soon > 0`
   - Action: Decide roll/close strategy

**Warning Alerts** (Email):

1. **Daily Limit Approaching**
   - Trigger: `ai_hedging_daily_contracts > 45`
   - Action: Monitor closely

2. **Wide Spreads**
   - Trigger: Average spread > 50 bps
   - Action: Consider tighter filters or different instruments

3. **Slow Selection**
   - Trigger: p95 latency > 10s
   - Action: Check IBKR API performance

---

## Troubleshooting Guide

### Issue: No Hedges Triggering

**Symptoms**: No `selection` events in `hedging_events.jsonl`

**Diagnosis**:
```bash
# Check if options hedging is enabled
grep -A 10 "options:" config.yml

# Check kill-switch triggers
cat runs/ai_trader/$(date +%Y-%m-%d)/risk_monitor.jsonl | \
  jq 'select(.hedge_needed)'
```

**Possible Causes**:
1. `options.enabled: false` in config
2. No kill-switch triggers (market calm)
3. `auto_execute: false` (manual review mode)

**Fix**:
- Enable in config if intended
- Check `risk_monitor.jsonl` for trigger conditions
- Set `auto_execute: true` after validation

---

### Issue: High Rejection Rate

**Symptoms**: Many `rejection` events, low fill rate

**Diagnosis**:
```bash
# Check rejection reasons
cat runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl | \
  jq 'select(.event_type=="rejection") | .details.reason' | \
  sort | uniq -c
```

**Common Reasons**:
- `daily_limit`: Already hit daily limit
- `spread_too_wide`: Illiquid contracts (increase `max_spread_bps`)
- `low_oi`: Low open interest (lower `min_open_interest`)
- `validation_failed`: Pre-execution checks failed
- `no_contracts`: No matching contracts in chain

**Fix**:
- If `daily_limit`: Wait until next day or request limit increase
- If `spread_too_wide`: Increase `max_spread_bps` to 40-50
- If `low_oi`: Lower `min_open_interest` to 3000-5000
- If `validation_failed`: Check IBKR connection, retry
- If `no_contracts`: Adjust `dte_preference`, `delta_target_abs`

---

### Issue: Orders Not Filling

**Symptoms**: Orders stuck in SUBMITTED for >10 min

**Diagnosis**:
```sql
SELECT
  id,
  symbol,
  limit_price,
  state,
  AGE(NOW(), created_at) as age
FROM core.core_order
WHERE strategy_id = 'hedge_overlay'
  AND state IN ('SUBMITTED', 'PARTIAL_FILLED')
ORDER BY created_at DESC;
```

**Possible Causes**:
1. Limit price too aggressive (below bid for buy)
2. Low liquidity during pre/post market
3. IBKR order rejected (check TWS logs)
4. Market moved away from limit

**Fix**:
```bash
# Cancel and re-trigger with higher slippage
python scripts/ai_trader/risk/cancel_hedge_orders.py --order-id <ID>

# Adjust slippage in config
# slippage_bps: 15 → 25
```

---

### Issue: IBKR Connection Failed

**Symptoms**: `Failed to connect to IBKR` in logs

**Diagnosis**:
```bash
# Check Gateway is running
ps aux | grep -i gateway

# Test connection
telnet 127.0.0.1 4002

# Check API settings
# Open Gateway → Configure → API → Settings
```

**Possible Causes**:
1. Gateway not running
2. Wrong port (4002 for paper, 4001 for live)
3. API not enabled
4. Client ID conflict

**Fix**:
1. Restart Gateway
2. Verify port in `config.yml`: `ibkr_port: 4002`
3. Enable API in Gateway settings
4. Change client_id in provider (auto-generates new ID on conflict)

---

### Issue: Stale Position Metrics

**Symptoms**: Grafana position gauges not updating

**Diagnosis**:
```bash
# Check metrics updater is running
ps aux | grep update_hedging_metrics

# Check Prometheus scrape
curl localhost:8000/metrics | grep ai_hedging_positions_open
```

**Possible Causes**:
1. Metrics updater crashed
2. `options_positions.json` not writable
3. IBKR market data unavailable

**Fix**:
```bash
# Restart metrics updater
pkill -f update_hedging_metrics
python scripts/ai_trader/risk/update_hedging_metrics.py --loop &

# Manually update
python scripts/ai_trader/risk/manage_positions.py --update-data
```

---

## Emergency Procedures

### Emergency Stop (Kill Switch)

**Use Case**: Immediate halt of all hedging activity

**Steps**:
1. Disable auto-execution:
   ```bash
   # Edit config.yml
   risk_management.hedge.auto_execute: false
   ```

2. Cancel all pending orders:
   ```bash
   python scripts/ai_trader/risk/cancel_hedge_orders.py --all --confirm
   ```

3. Restart orchestrator:
   ```bash
   pkill -f orchestrator.py
   # Wait for restart via supervisor/systemd
   ```

4. Verify no new hedges:
   ```bash
   tail -f runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl
   # Should see no new events
   ```

---

### Emergency Position Close

**Use Case**: Must close all option positions immediately

**Steps**:
1. List all open positions:
   ```bash
   python scripts/ai_trader/risk/manage_positions.py --list
   ```

2. **Manual TWS**: Open IBKR TWS, submit closing orders:
   - For long positions: SELL TO CLOSE at market
   - For short positions: BUY TO CLOSE at market

3. Update tracker after fills:
   ```python
   from scripts.ai_trader.risk.options_positions import get_position_tracker
   tracker = get_position_tracker('runs/ai_trader')

   # For each closed position:
   tracker.add_position('SPY   251116P00450000', -5, fill_price, 0)
   # Negative quantity to close
   ```

4. Verify all closed:
   ```bash
   python scripts/ai_trader/risk/manage_positions.py --summary
   # total_positions should be 0
   ```

---

### Rollback to Stub Provider

**Use Case**: IBKR integration failing, revert to stub for testing

**Steps**:
1. Edit `config.yml`:
   ```yaml
   risk_management:
     hedge:
       options:
         data_provider: stub  # Change from 'ibkr'
   ```

2. Restart orchestrator:
   ```bash
   pkill -f orchestrator.py
   ```

3. Verify using stub:
   ```bash
   cat runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl | \
     jq '.details.backend'
   # Should show "stub"
   ```

---

## Maintenance Tasks

### Weekly Tasks

**1. Review Hedge Effectiveness**
```sql
-- Calculate hedge correlation with portfolio drawdowns
-- Check if hedges actually reduced volatility
-- TODO: Automated hedge effectiveness report
```

**2. Clean Up Closed Positions**
```bash
# Archive old positions (DTE < 0 or quantity = 0)
# TODO: Position archival script
```

**3. Update Documentation**
- Review this runbook for accuracy
- Update alert thresholds based on observations
- Document new edge cases

---

### Monthly Tasks

**1. Review Daily Limits**
- Analyze utilization over past month
- Adjust limits if consistently hitting cap or underutilized

**2. Optimize Filters**
- Review average spread, OI, delta error
- Tighten or loosen filters based on data

**3. Performance Review**
- Fill rate trend
- Slippage analysis
- Hedge P&L attribution

---

### Quarterly Tasks

**1. IBKR Position Reconciliation**
- Compare tracked positions with IBKR account
- Investigate discrepancies
- Implement reconciliation API (Phase 3)

**2. Disaster Recovery Drill**
- Test emergency stop procedure
- Test position close procedure
- Test failover to stub provider

**3. Upgrade Planning**
- Review Phase 3 roadmap
- Prioritize new features based on ops feedback

---

## Appendices

### A. File Locations

| File | Purpose | Location |
|------|---------|----------|
| Config | System configuration | `config.yml:234-255` |
| Position Tracker | Open positions JSON | `runs/ai_trader/options_positions.json` |
| Hedging Events | Audit log | `runs/ai_trader/YYYY-MM-DD/hedging_events.jsonl` |
| Hedge Plans | Daily hedge selection | `runs/ai_trader/YYYY-MM-DD/options_hedge.json` |
| Orders Table | DB order records | `core.core_order` (PostgreSQL) |

---

### B. Contact Information

| Role | Contact | Escalation |
|------|---------|------------|
| On-Call Ops | ops-oncall@example.com | Immediate |
| Trading Manager | trading-mgr@example.com | Within 1 hour |
| Engineering | eng-trading@example.com | Next business day |

---

### C. Related Documentation

- [Options Hedging Implementation](OPTIONS_HEDGING_IMPLEMENTATION.md)
- [Phase 2 Deployment Guide](OPTIONS_PHASE2_DEPLOYMENT.md)
- [Metrics Guide](OPTIONS_METRICS_GUIDE.md)
- [CLAUDE.md](../CLAUDE.md) - General system guidelines

---

**Last Updated**: 2025-11-02
**Version**: 1.0
**Maintainer**: Trading Operations Team
