# Options Hedging Phase 2 Deployment Guide

**Date**: 2025-11-02
**Target**: IBKR Paper Trading with Real Option Data
**Estimated Duration**: 2-3 days validation → 1 week pilot
**Risk Level**: LOW-MEDIUM (Paper account, small position sizes)

---

## Overview

Phase 2 enables **real options execution** using IBKR option chain data and live market quotes. The system will:

1. ✅ Fetch real option chains from IBKR Gateway/TWS
2. ✅ Select contracts based on live bid/ask/OI/Greeks
3. ✅ Validate pre-execution with current market data
4. ✅ Submit limit orders to IBKR Paper account
5. ✅ Monitor fills and hedge effectiveness

---

## Prerequisites

### 1. IBKR Setup

**Gateway/TWS Configuration**:
- [ ] Install IBKR Gateway or TWS (latest version)
- [ ] Enable Paper Trading mode
- [ ] Configure API settings:
  - ✅ Enable ActiveX and Socket Clients
  - ✅ Socket Port: **4002** (Paper Trading)
  - ✅ Allow connections from localhost (127.0.0.1)
  - ✅ Read-Only API: **NO** (need order submission)
  - ✅ Download open orders on connection: YES

**Account Requirements**:
- [ ] Active IBKR Paper Trading account
- [ ] Options trading enabled
- [ ] Sufficient virtual capital ($100k+ recommended)

**Permissions**:
```
Required permissions:
- Read market data (Level 1)
- Options market data
- Submit orders
- Read account info
- Read positions
```

### 2. Software Dependencies

```bash
# Already installed in venv
pip list | grep ib-insync
# ib-insync==0.9.86 (or similar)

# If missing:
pip install ib-insync
```

### 3. Network & Connectivity

```bash
# Test IBKR Gateway connection
telnet 127.0.0.1 4002
# Should connect (Ctrl+C to exit)

# Check if Gateway is running
ps aux | grep -i tws
# or
ps aux | grep -i gateway
```

---

## Configuration Changes

### Step 1: Update `config.yml`

**Location**: `./config.yml`

```yaml
risk_management:
  hedge:
    auto_execute: false  # ⚠️ Keep false initially for manual review
    mode: both  # or 'options' only
    options:
      enabled: true
      data_provider: ibkr  # ✅ Change from 'stub' to 'ibkr'
      ibkr_host: 127.0.0.1
      ibkr_port: 4002  # Paper trading port
      instruments: [SPY]  # Start with SPY only
      primary: delta
      delta_target_abs: 0.4
      dte_preference: [7, 14]  # Shorter DTEs for testing
      min_open_interest: 5000  # Higher threshold for liquidity
      max_spread_bps: 25  # Tighter spread requirement
      min_last_trade_minutes: 5
      slippage_bps: 15  # Slightly higher for real execution
      tif: DAY
      max_contracts_per_day: 3  # ⚠️ Very conservative for Phase 2
      max_notional_usd_per_day: 15000  # ⚠️ Small limit
      dry_run: false  # ✅ Enable real execution (paper account)
```

**Key Changes**:
1. `data_provider: ibkr` - Use real IBKR data
2. `dry_run: false` - Allow actual order submission
3. `max_contracts_per_day: 3` - Tight limit for testing
4. `max_notional_usd_per_day: 15000` - Conservative cap
5. `instruments: [SPY]` - Single liquid instrument

---

## Deployment Steps

### Phase 2A: Validation (Days 1-2)

**Goal**: Verify IBKR integration works without triggering hedges

#### Step 1: Test IBKR Connection

```bash
cd .

# Test option chain fetch
PYTHONPATH=. venv/bin/python -c "
import asyncio
from services.market.options_data import create_options_provider

async def test():
    provider = create_options_provider('ibkr', host='127.0.0.1', port=4002)
    chain = await provider.get_chain('SPY', [7, 14], 'P')
    print(f'Fetched {len(chain)} contracts')
    if chain:
        print(f'Sample: {chain[0].symbol}, bid={chain[0].bid}, ask={chain[0].ask}, OI={chain[0].open_interest}')
    await provider.disconnect()

asyncio.run(test())
"
```

**Expected Output**:
```
Fetched 40 contracts
Sample: SPY   251109P00450000, bid=5.10, ask=5.15, OI=12543
```

**If Fails**:
- Check Gateway is running: `ps aux | grep -i gateway`
- Check port 4002 is open: `netstat -an | grep 4002`
- Check IBKR API settings enabled
- Check firewall not blocking localhost connections

#### Step 2: Test Contract Selection

```bash
# Temporarily trigger selection (without executing)
PYTHONPATH=. venv/bin/python scripts/ai_trader/risk/options_selector.py
```

**Verify Output**:
```json
{
  "need_hedge": true,
  "contract": {
    "occ_symbol": "SPY   251109P00450000",  // Real OCC symbol
    "delta": -0.42,                          // Real Greek from IBKR
    "open_interest": 12543,                  // Real OI
    "spread_bps": 9.8,                       // Real spread
    ...
  }
}
```

**Success Criteria**:
- ✅ Valid OCC symbol format
- ✅ Delta from IBKR Greeks (not synthetic)
- ✅ Real OI > 5000
- ✅ Spread < 25bps
- ✅ Contract appears in `runs/ai_trader/YYYY-MM-DD/options_hedge.json`

#### Step 3: Test Pre-Execution Validation

```bash
# Run executor in dry-run mode (even though config says false, test manually)
PYTHONPATH=. venv/bin/python scripts/ai_trader/portfolio/execute_hedge.py \
  --runs-dir runs/ai_trader \
  --mode options \
  # Don't use --execute flag (dry-run)
```

**Check Logs**:
```bash
cat runs/ai_trader/$(date +%Y-%m-%d)/hedge_exec.jsonl | tail -1 | jq '.'
```

**Expected**:
```json
{
  "ts": "2025-11-02",
  "symbol": "SPY   251109P00450000",
  "side": "BUY",
  "qty": 2,
  "limit_px": 5.20,
  "order_type": "OPT_LMT",
  "mode": "options_real",
  "dry_run": true,  // Should be true without --execute
  "validation": {
    "valid": true,
    "live_mid": 5.125,
    "live_spread_bps": 9.8
  }
}
```

**Success Criteria**:
- ✅ Validation passed with live IBKR data
- ✅ Price drift < 10%
- ✅ Spread still acceptable

---

### Phase 2B: Pilot Execution (Days 3-9)

**Goal**: Submit real orders to IBKR Paper account and monitor

#### Step 1: Enable Auto-Execution (Manual First)

**First Order (Manual)**:
```bash
# Trigger a hedge manually to test
PYTHONPATH=. venv/bin/python scripts/ai_trader/portfolio/execute_hedge.py \
  --runs-dir runs/ai_trader \
  --mode options \
  --execute  # ⚠️ Real execution flag
```

**Monitor**:
```bash
# Check if order submitted to DB
psql -h localhost -U trading_user -d trading_db -c \
  "SELECT symbol, side, order_type, qty, limit_price, state, note
   FROM core.core_order
   WHERE strategy_id='hedge_overlay'
   AND order_type='OPT_LMT'
   ORDER BY created_at DESC LIMIT 5;"
```

**Check IBKR TWS/Gateway**:
- Open Orders tab → should see option limit order
- Symbol: OCC format (e.g., SPY   251109P00450000)
- Action: BUY
- Quantity: 1-3 contracts
- Order Type: LMT
- Limit Price: as specified

#### Step 2: Monitor Fill & Execution

**Wait 5-10 minutes for fill**:
```bash
# Check order state
psql -h localhost -U trading_user -d trading_db -c \
  "SELECT symbol, state, filled_qty, avg_fill_price, updated_at
   FROM core.core_order
   WHERE idempotency_key LIKE 'hedge:%'
   ORDER BY updated_at DESC LIMIT 5;"
```

**Expected States**:
- `NEW` → `SUBMITTED` → `PARTIAL_FILLED` or `FILLED`
- If `REJECTED`: check IBKR logs for reason

**In IBKR TWS**:
- Check Executed Orders
- Verify fill price ≤ limit price
- Check account positions (should show +N SPY PUT contracts)

#### Step 3: Enable Auto-Execution

**After 3 successful manual executions**:

```yaml
# config.yml
risk_management:
  hedge:
    auto_execute: true  # ✅ Enable automatic execution
```

**Restart Orchestrator**:
```bash
# If running
pkill -f "orchestrator.py"

# Start with new config
venv/bin/python scripts/ai_trader/orchestrator.py --runs-dir runs/ai_trader
```

**Monitor Automated Hedges**:
```bash
# Watch for kill-switch triggers
tail -f runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl
```

---

## Validation Checklist

### Daily Checks (Week 1)

**Morning (Pre-Market)**:
- [ ] IBKR Gateway running and connected
- [ ] Check yesterday's hedge fills:
  ```sql
  SELECT COUNT(*), AVG(avg_fill_price/limit_price) as fill_quality
  FROM core.core_order
  WHERE strategy_id='hedge_overlay'
    AND order_type='OPT_LMT'
    AND state IN ('FILLED', 'PARTIAL_FILLED')
    AND created_at > CURRENT_DATE - INTERVAL '1 day';
  ```
- [ ] Verify no limit breaches:
  ```bash
  cat runs/ai_trader/$(date -d yesterday +%Y-%m-%d)/hedging_events.jsonl | \
    jq 'select(.event_type=="rejection" and .details.reason | contains("limit"))'
  ```

**Intraday**:
- [ ] Monitor hedge triggers (DrawDown, vol spike):
  ```bash
  tail -f runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl | \
    grep -E '(selection|execution)'
  ```
- [ ] Check for validation failures:
  ```bash
  cat runs/ai_trader/$(date +%Y-%m-%d)/hedge_exec.jsonl | \
    jq 'select(.action=="skip_validation_failed")'
  ```

**End of Day**:
- [ ] Review daily stats:
  ```python
  from scripts.ai_trader.risk.hedging_events import HedgingEventsLogger
  logger = HedgingEventsLogger('runs/ai_trader')
  stats = logger.get_daily_stats()
  print(stats)
  ```
- [ ] Check IBKR account P&L from hedges
- [ ] Verify positions reconcile with orders

---

## Success Metrics (Week 1)

**Must Achieve**:
1. ✅ **Fill Rate ≥ 70%** within 10 minutes
2. ✅ **Average Slippage ≤ 20 bps** (fill vs limit)
3. ✅ **Zero daily limit breaches**
4. ✅ **All validation passes or fails safely**
5. ✅ **No manual intervention required**

**Quality Metrics**:
- Average spread of selected contracts ≤ 20 bps
- Average OI ≥ 8000
- Delta error ≤ 0.05 (|selected Δ - 0.40|)
- No stale contracts (last trade < 5 min)

**Hedge Effectiveness** (if triggered):
- Portfolio delta reduction ≥ 50% after hedge
- Hedge cost ≤ 0.5% of gross exposure
- No adverse selection (hedges don't immediately lose value)

---

## Troubleshooting

### Issue: "Failed to connect to IBKR"

**Symptoms**:
```
RuntimeError: Failed to connect to IBKR: ConnectionRefusedError
```

**Fixes**:
1. Check Gateway is running: `ps aux | grep -i gateway`
2. Restart Gateway/TWS
3. Verify port: `netstat -an | grep 4002` should show LISTEN
4. Check API settings in Gateway Config → API → Settings
5. Try different client_id in code (avoid conflicts)

### Issue: "No contracts found in current chain"

**Symptoms**:
```
{"valid": false, "reason": "Contract not found in current chain"}
```

**Fixes**:
1. Check if market is open (options trade 9:30 AM - 4:00 PM ET)
2. Verify underlying symbol is correct (SPY not SP500)
3. Check expiry date hasn't passed
4. Increase DTE tolerance in selection

### Issue: "Spread exceeds max"

**Symptoms**:
```
{"valid": false, "reason": "Spread 45.2bps exceeds max 25bps"}
```

**Fixes**:
1. Market may be illiquid - wait for RTH (9:30 AM - 4:00 PM ET)
2. Temporarily increase `max_spread_bps` to 40-50
3. Use only SPY/QQQ (most liquid)
4. Avoid far OTM strikes (increase `delta_target_abs` to 0.45)

### Issue: Orders rejected by IBKR

**Symptoms**:
```
Order state: REJECTED
```

**Check**:
1. IBKR TWS Activity Log for rejection reason
2. Common reasons:
   - No permissions for options trading
   - Order size exceeds limits
   - Invalid contract specification
   - Market closed

**Fix**:
- Enable options permissions in Account Management
- Reduce order size
- Verify OCC symbol format is correct

---

## Rollback Procedure

**If Issues Arise**:

### Immediate Stop (< 1 minute):
```bash
# 1. Disable auto-execution
vim config.yml
# Set: risk_management.hedge.auto_execute: false

# 2. Switch back to stub
# Set: risk_management.hedge.options.data_provider: stub

# 3. Restart orchestrator
pkill -f "orchestrator.py"
venv/bin/python scripts/ai_trader/orchestrator.py --runs-dir runs/ai_trader &
```

### Cancel Open Orders (if needed):
```bash
# Via IBKR TWS: Orders → Cancel All

# Or via API (if implemented):
# python scripts/cancel_all_hedge_orders.py
```

### Revert to Phase 1:
```yaml
# config.yml
risk_management:
  hedge:
    options:
      enabled: false  # Disable options hedging entirely
```

---

## Phase 2 → Phase 3 Transition

**After 1 Week Successful Pilot**:

**Graduation Criteria**:
- ✅ 10+ successful hedge executions
- ✅ Fill rate ≥ 80%
- ✅ Slippage ≤ 15 bps
- ✅ Zero system errors
- ✅ Hedge effectiveness validated

**Phase 3 Changes**:
```yaml
risk_management:
  hedge:
    options:
      max_contracts_per_day: 10  # Increase gradually
      max_notional_usd_per_day: 50000  # Increase gradually
      instruments: [SPY, QQQ]  # Add QQQ
      dte_preference: [7, 14, 30]  # Add 30 DTE
```

**New Features for Phase 3**:
- Position tracking system
- Automated roll management (expiring contracts)
- Grafana monitoring dashboard
- Alerts for repeated failures

---

## Contact & Support

**Escalation Path**:
1. Check logs: `runs/ai_trader/YYYY-MM-DD/hedging_events.jsonl`
2. Review IBKR logs: TWS → Help → Activity Log
3. Consult: `docs/OPTIONS_HEDGING_IMPLEMENTATION.md`
4. Rollback if needed (see above)

**Key Files**:
- Config: `config.yml:234-255`
- Selector: `scripts/ai_trader/risk/options_selector.py`
- Executor: `scripts/ai_trader/portfolio/execute_hedge.py`
- Data Provider: `services/market/options_data.py:254-512`

---

**Ready to Deploy**: Phase 2A validation can start immediately once IBKR Gateway is configured.
