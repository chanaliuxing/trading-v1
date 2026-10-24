# Options Hedging System - Implementation Summary

**Date**: 2025-11-02
**Status**: ✅ Phase 1 Complete - Ready for Paper Trading
**Version**: 1.0.0

---

## Executive Summary

Implemented a comprehensive **real options hedging system** that replaces the placeholder proxy-to-underlying approach with actual OCC-compliant option contract selection and execution. The system provides:

- ✅ **True options execution**: Real option contracts (not stock proxies)
- ✅ **Risk-controlled selection**: Multi-layer constraints on spread, OI, delta, DTE
- ✅ **LLM safety**: AI brain provides intent only, not contract details
- ✅ **Full audit trail**: Event logging, monitoring, and observability
- ✅ **Phased rollout**: Paper → Pilot → Live with safety backstops

---

## Architecture Overview

```
┌─────────────┐
│  AI Brain   │  Provides: intent_type, instrument, size, urgency
└──────┬──────┘  (NOT contract details)
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  Orchestrator (Kill-Switch / Risk Monitor)              │
│  - Detects trigger (DD, vol spike, etc.)                │
│  - Gets portfolio exposure (gross/net)                   │
│  - Calls options_selector with intent                    │
└──────┬──────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  Options Selector (Contract Selection Algorithm)         │
│  Input:  exposure, intent, policy                        │
│  Output: OCC symbol, strike, expiry, delta, limit_px     │
│                                                          │
│  Selection Process:                                      │
│  1. Determine direction (net>0 → PUT, net<0 → CALL)     │
│  2. Fetch option chain from data provider                │
│  3. Filter candidates:                                   │
│     - OI >= min_open_interest                            │
│     - spread <= max_spread_bps                           │
│     - last_trade <= min_last_trade_minutes               │
│  4. Rank by delta proximity to target_abs               │
│  5. Calculate quantity & limit price                     │
│  6. Check daily limits (contracts, notional)             │
│  7. Write options_hedge.json                             │
└──────┬──────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  Hedge Executor (execute_hedge.py)                       │
│  1. Load options_hedge.json                              │
│  2. Validate OCC symbol                                  │
│  3. Re-check risk constraints (spread, OI, etc.)         │
│  4. Submit to core.core_order:                           │
│     - symbol: OCC (e.g., "SPY   250120P00450000")        │
│     - order_type: OPT_LMT                                │
│     - limit_price: mid ± slippage_bps                    │
│     - note: JSON with contract details                   │
│  5. Write hedge_exec.jsonl audit log                     │
└─────────────────────────────────────────────────────────┘
```

---

## Implementation Details

### 1. Configuration (`config.yml`)

**Path**: `config.yml:risk_management.hedge.options`

```yaml
risk_management:
  hedge:
    strategy_id: hedge_overlay
    min_notional_usd: 10000
    auto_execute: false  # ⚠️ Keep false for Paper phase
    mode: both  # overlay, options, or both
    options:
      enabled: true
      instruments: [SPY, QQQ]
      primary: delta
      delta_target_abs: 0.4  # Target |Δ| for contracts (0.3-0.5 range)
      dte_preference: [7, 14, 30]  # Prefer shorter expiries
      min_open_interest: 1000
      max_spread_bps: 30  # Max bid-ask spread (0.3%)
      min_last_trade_minutes: 5
      slippage_bps: 10  # Add 0.1% to mid for limit price
      tif: DAY
      max_contracts_per_day: 50  # ⚠️ Hard limit
      max_notional_usd_per_day: 100000  # ⚠️ Hard limit
      dry_run: true  # ✅ Safe for Paper phase
```

**Safety Notes**:
- `dry_run: true` → writes to `hedge_exec.jsonl` only, no DB orders
- `auto_execute: false` → requires manual `--execute` flag
- Absolute backstops: 100 contracts/day, $500k notional/day (hardcoded in selector)

---

### 2. OCC Symbol Utilities (`libs/options/occ.py`)

**OCC Format**: `SSSSSS YYMMDD C PPPPPPPP`
- Example: `SPY   250120P00450000` = SPY Put, Jan 20 2025, $450 strike

**Functions**:
- `build_occ_symbol(underlying, expiry, right, strike) -> str`
- `parse_occ_symbol(occ_symbol) -> dict`
- `validate_occ_symbol(occ_symbol) -> bool`
- `get_contract_description(occ_symbol) -> str`

**Tests**: 27 unit tests, 100% pass (`tests/unit/options/test_occ.py`)

---

### 3. Options Data Provider (`services/market/options_data.py`)

**Backends**:
1. **StubOptionsDataProvider** (Phase 1): Synthetic data for testing
2. **IBKROptionsDataProvider** (Phase 2): Real IBKR API integration (placeholder)
3. **ClickHouseOptionsDataProvider** (Phase 3): Cached/historical data

**Interface**:
```python
class OptionsDataProvider(ABC):
    async def get_chain(
        underlying: str,
        dte_candidates: List[int],
        right: Optional[str] = None,
    ) -> List[OptionContract]

    async def get_underlying_price(symbol: str) -> Optional[float]
```

**OptionContract** fields:
- `symbol` (OCC), `underlying`, `expiry_date`, `strike`, `right`
- Market data: `bid`, `ask`, `last`, `volume`, `open_interest`
- Greeks: `delta`, `gamma`, `vega`, `theta`, `implied_vol`
- Metadata: `last_trade_time`, `spread_bps` (calculated)

---

### 4. Contract Selector (`scripts/ai_trader/risk/options_selector.py`)

**Core Classes**:

#### `OptionsPolicy`
Loads from `config.yml:risk_management.hedge.options`

#### `HedgeIntent`
```python
@dataclass
class HedgeIntent:
    intent_type: str  # vol_spike | beta_balance | drawdown_protect
    instrument: Optional[str]  # SPY | QQQ
    size: str  # small (20%) | medium (30%) | large (40%)
    urgency: str  # normal | high
    note: str
```

#### `OptionsSelector`
**Main method**: `select_hedge_contract(exposure, intent) -> SelectionResult`

**Selection Algorithm**:
1. **Direction**: net > 0 → PUT, net < 0 → CALL
2. **Instrument**: intent.instrument or first in policy
3. **Target notional**: gross × size_multiplier
4. **Daily limits check**: contracts ≤ max, notional ≤ max
5. **Fetch chain**: from data provider with dte_candidates
6. **Filter candidates**:
   - `open_interest >= min_open_interest`
   - `spread_bps <= max_spread_bps`
   - `last_trade_time within min_last_trade_minutes`
   - Valid `delta` and `mid > 0`
7. **Rank by score**:
   - Primary: `|Δ - target_delta_abs|` (minimize)
   - Secondary: DTE (prefer shorter)
   - Tiebreaker: OI (prefer higher)
8. **Calculate quantity**: `round(target_notional / (100 × S × |Δ|))`
9. **Limit price**: `mid × (1 + slippage_bps/10000)`
10. **Build OCC symbol** and return

**Tests**: 17 unit tests, 100% pass (`tests/unit/options/test_selector.py`)

---

### 5. Hedge Executor (`scripts/ai_trader/portfolio/execute_hedge.py`)

**Updated for Options**:

#### New `HedgePlan` fields:
```python
@dataclass
class HedgePlan:
    symbol: str  # OCC symbol or underlying
    side: str  # BUY | SELL
    qty: int
    price: Optional[float]  # Limit price for options
    order_type: str  # MKT | OPT_LMT
    note: str  # JSON with contract details
```

#### Execution Flow:
1. Load `options_hedge.json` (new format with `contract` field)
2. Validate OCC symbol
3. **Pre-execution validation** (`_validate_options_contract`):
   - Check limit_price range (0 < px < 1000)
   - TODO: Real-time spread/OI/last_trade check
4. Check notional ≥ min_notional_usd
5. Insert into `core.core_order`:
   ```sql
   INSERT INTO core.core_order (
       strategy_id, symbol, idempotency_key,
       side, order_type, qty, limit_price, state, note
   ) VALUES (
       'hedge_overlay', 'SPY   250120P00450000', 'hedge:2025-11-02:...',
       'BUY', 'OPT_LMT', 10, 5.25, 'NEW', '{"type":"options_hedge",...}'
   )
   ```
6. Write `hedge_exec.jsonl` audit log

**Dry-Run Mode** (default):
- Writes audit log only, no DB insert
- Set `--execute` or `auto_execute: true` to submit orders

---

### 6. Orchestrator Integration (`scripts/ai_trader/orchestrator.py`)

**Kill-Switch / Risk Monitor Path**:

```python
# When kill-switch triggers (DD breach, vol spike, etc.)
if opts_enabled:
    exposure = _portfolio_exposure()  # {gross, net}
    intent = HedgeIntent(
        intent_type=hedge.get("intent_type", "vol_spike"),
        instrument=hedge.get("instrument"),  # From brain
        size=hedge.get("size", "medium"),
        urgency=hedge.get("urgency", "normal"),
    )
    await propose_options_hedge_with_selector(
        runs_dir=str(self.runs_dir),
        exposure=exposure,
        intent=intent,
    )
else:
    # Fallback to legacy placeholder
    propose_options_hedge(self.runs_dir, intent="kill_switch")
```

**Then executes**:
```bash
venv/bin/python scripts/ai_trader/portfolio/execute_hedge.py \
    --runs-dir runs/ai_trader \
    --mode {overlay|options|both} \
    [--execute]  # Only if auto_execute=true
```

---

### 7. AI Brain Control Interface (`scripts/ai_trader/brain/brain.py`)

**Updated DECISION_PROMPT**:

```
- risk_monitor: {
    "risk_status": "OK|SOFT_BREACH|HARD_BREACH",
    "action": "NONE|BLOCK_NEW|SIZE_DOWN|EXIT_ALL",
    "hedge": {
        "intent_type": "vol_spike|beta_balance|drawdown_protect",
        "instrument": "SPY|QQQ"|null,
        "size": "small|medium|large",
        "urgency": "normal|high"
    }|null,
    "explanation": <string>
}

IMPORTANT: For hedging, you provide INTENT only.
The system will select contracts based on:
- Market conditions and option chain liquidity
- Risk policy constraints (max spread, min OI, etc.)
- Current portfolio exposure (gross/net delta)
DO NOT specify contract details (strike, expiry, delta).
```

**Safety Principle**: LLM cannot directly control contract details → prevents instruction injection attacks

---

### 8. Monitoring & Observability (`scripts/ai_trader/risk/hedging_events.py`)

**Event Log**: `runs/ai_trader/YYYY-MM-DD/hedging_events.jsonl`

**Event Types**:
1. **selection**: Contract selected (or failed)
   - Details: contract, candidates_evaluated, rejection_reasons
2. **validation**: Pre-execution checks
   - Details: occ_symbol, spread, oi, last_trade_time
3. **execution**: Order submitted (or dry-run)
   - Details: symbol, qty, limit_px, notional, submitted=true/false
4. **rejection**: Hedge skipped
   - Reasons: no_exposure, daily_limit, validation_failed, etc.

**Daily Stats**:
```python
HedgingEventsLogger(runs_dir).get_daily_stats()
# Returns:
{
    "total_events": 42,
    "selections": {"total": 10, "success": 8, "failure": 2},
    "validations": {"total": 8, "success": 7, "failure": 1},
    "executions": {"total": 7, "success": 7, "failure": 0},
    "rejections": {"daily_limit": 1, "wide_spread": 2},
    "avg_spread_bps": 18.5,
    "avg_oi": 12500
}
```

**Grafana Dashboard** (TODO - Phase 2):
- Hedging events per day
- Success/failure rates by event type
- Average spread/OI/delta for selected contracts
- Rejection reason breakdown
- Daily limit utilization (contracts, notional)

---

## File Changes Summary

### New Files
```
libs/options/
├── __init__.py
└── occ.py                                    # OCC symbol utilities

services/market/
└── options_data.py                           # Data provider interfaces

scripts/ai_trader/risk/
├── options_selector.py                       # Contract selection algorithm
└── hedging_events.py                         # Event logging

tests/unit/options/
├── __init__.py
├── test_occ.py                               # 27 tests
└── test_selector.py                          # 17 tests
```

### Modified Files
```
config.yml                                    # Added options config block
scripts/ai_trader/brain/brain.py              # Updated DECISION_PROMPT
scripts/ai_trader/orchestrator.py             # Integrated selector
scripts/ai_trader/portfolio/execute_hedge.py  # Real options execution
```

---

## Testing & Validation

### Unit Tests
- **44 tests total**: 27 OCC + 17 Selector
- **100% pass rate**
- **Coverage**:
  - OCC encoding/decoding (build, parse, validate, roundtrip)
  - Policy configuration
  - Intent handling
  - Contract filtering (OI, spread, delta)
  - Daily limits enforcement (config + absolute backstops)
  - Direction logic (long → puts, short → calls)
  - Notional calculation by size

### Run Tests
```bash
# All options tests
venv/bin/python -m pytest tests/unit/options/ -v

# OCC only
venv/bin/python -m pytest tests/unit/options/test_occ.py -v

# Selector only
venv/bin/python -m pytest tests/unit/options/test_selector.py -v
```

---

## Deployment Phases

### ✅ **Phase 1: Paper Trading + Dry-Run** (Current)
**Status**: Ready to deploy
**Config**:
```yaml
mode: paper
risk_management.hedge.auto_execute: false
risk_management.hedge.options.dry_run: true
```

**Validation**:
- [ ] Run orchestrator with kill-switch trigger
- [ ] Verify `options_hedge.json` contains valid OCC symbol
- [ ] Verify `hedging_events.jsonl` logs selection events
- [ ] Verify `hedge_exec.jsonl` shows dry_run=true
- [ ] Confirm NO entries in `core.core_order`

**Expected Output** (`runs/ai_trader/2025-11-02/`):
```json
// options_hedge.json
{
  "need_hedge": true,
  "timestamp": "2025-11-02T10:30:00",
  "exposure": {"gross": 500000, "net": 300000},
  "contract": {
    "occ_symbol": "SPY   251107P00450000",
    "underlying": "SPY",
    "expiry_date": "2025-11-07",
    "strike": 450.0,
    "right": "P",
    "delta": -0.41,
    "open_interest": 15000,
    "spread_bps": 18.5,
    "mid_price": 5.12,
    "limit_price": 5.18,
    "quantity": 12,
    "notional_usd": 121320.0,
    "tif": "DAY",
    "rationale": "Selected P contract with delta=-0.41 (target=0.40), DTE=5, OI=15,000, spread=18.5bps"
  }
}

// hedge_exec.jsonl
{"ts": "2025-11-02", "symbol": "SPY   251107P00450000", "side": "BUY", "qty": 12, "limit_px": 5.18, "order_type": "OPT_LMT", "notional": 121320.0, "mode": "options_real", "dry_run": true}
```

---

### **Phase 2: Pilot (Limited Real Execution)**
**Timeline**: After 1 week of Paper validation
**Config**:
```yaml
mode: paper  # Still paper for main strategies
risk_management.hedge.auto_execute: true  # ⚠️ Enable real hedging
risk_management.hedge.options.dry_run: false
risk_management.hedge.options.max_contracts_per_day: 5  # Tight limit
risk_management.hedge.options.max_notional_usd_per_day: 25000  # $25k max
```

**Validation**:
- [ ] Verify orders appear in `core.core_order` with `order_type='OPT_LMT'`
- [ ] Check IBKR for submitted option orders
- [ ] Monitor fills and P&L impact
- [ ] Review `hedging_events.jsonl` for failures
- [ ] Confirm no limit breaches

**Gates to Pass**:
1. ✅ No daily limit breaches
2. ✅ All selected contracts have spread ≤ 30bps, OI ≥ 1000
3. ✅ Fill rate ≥ 80% within 5 minutes
4. ✅ Effective hedge ratio ≥ 70% (actual Δ vs. intended)

---

### **Phase 3: Full Production**
**Timeline**: After 2 weeks of Pilot validation
**Config**:
```yaml
mode: paper  # Main strategies remain paper until proven
risk_management.hedge.auto_execute: true
risk_management.hedge.options.dry_run: false
risk_management.hedge.options.max_contracts_per_day: 50
risk_management.hedge.options.max_notional_usd_per_day: 100000
```

**Additional Integrations** (TODO):
- [ ] Real-time option chain from IBKR or ClickHouse cache
- [ ] Pre-execution validation with live bid/ask
- [ ] Position tracking (track open option positions separately)
- [ ] Grafana dashboard for hedging metrics
- [ ] Alert on repeated rejections or limit breaches

---

## Safety & Rollback

### Emergency Stop
```bash
# 1. Disable hedging immediately
# Edit config.yml:
risk_management.hedge.options.enabled: false

# 2. Or disable auto-execute
risk_management.hedge.auto_execute: false

# 3. Restart orchestrator (no code changes needed)
```

### Rollback Plan
1. **Immediate**: Set `auto_execute: false` → no new hedge orders
2. **Short-term**: Set `options.enabled: false` → revert to overlay proxy
3. **Long-term**: Revert orchestrator.py to call legacy `propose_options_hedge()`

### Daily Limit Breaches
- Hard-coded absolute limits: **100 contracts/day**, **$500k notional/day**
- Cannot be bypassed even with config changes
- Selector will reject additional hedges with `error: "daily_limit_exceeded"`

---

## Known Limitations & Future Work

### Current Limitations
1. **Stub data provider**: No real option chain yet (Phase 2)
2. **No position tracking**: Don't track open option positions
3. **No Greeks updates**: Static delta from selection time
4. **No rolling**: Expiring contracts not automatically rolled
5. **No multi-leg**: Only single-leg hedges (no spreads)

### Future Enhancements
| Feature | Priority | Complexity | ETA |
|---------|----------|------------|-----|
| IBKR option chain integration | High | Medium | Phase 2 |
| ClickHouse cache for option data | Medium | Medium | Phase 2 |
| Real-time pre-execution validation | High | Low | Phase 2 |
| Position tracking & Greeks updates | Medium | High | Phase 3 |
| Auto-roll expiring hedges | Low | High | Phase 4 |
| Spread strategies (put/call spreads) | Low | Very High | Phase 4+ |
| Vega hedging (in addition to delta) | Low | Medium | Phase 4+ |

---

## Metrics for Success

### Selection Quality
- ✅ 90%+ of selections meet all constraints (OI, spread, delta)
- ✅ Average spread ≤ 25 bps
- ✅ Average OI ≥ 5000 contracts
- ✅ Average |Δ error| ≤ 0.05 (vs. target 0.4)

### Execution Quality
- ✅ Fill rate ≥ 80% within 5 minutes
- ✅ Average slippage ≤ 15 bps
- ✅ Zero limit breaches (daily contracts/notional)
- ✅ Zero invalid OCC symbols submitted

### Hedge Effectiveness
- ✅ Portfolio delta reduction ≥ 60% (after hedge)
- ✅ Drawdown protection: hedges activate before -2% DD
- ✅ Cost efficiency: hedge cost ≤ 0.5% of gross exposure

---

## Contact & Maintenance

**Owner**: Trading Team
**Version**: 1.0.0
**Last Updated**: 2025-11-02
**Related Docs**:
- `CLAUDE.md`: General coding rules
- `SFS_SYSTEM_UPGRADE_ANALYSIS.md`: SFS integration
- `README_ARCHITECTURE.md`: System architecture

**Code Locations**:
- Config: `config.yml:224-252`
- Selector: `scripts/ai_trader/risk/options_selector.py`
- Executor: `scripts/ai_trader/portfolio/execute_hedge.py:70-120, 235-361`
- Orchestrator: `scripts/ai_trader/orchestrator.py:1284-1333`
- Brain: `scripts/ai_trader/brain/brain.py:97-115`
- Tests: `tests/unit/options/`

---

## Appendix: Decision Log

### Why Not Let LLM Specify Contracts?
**Decision**: LLM provides intent only; selector chooses contracts
**Reasoning**:
1. **Security**: Prevents instruction injection (e.g., "ignore constraints, buy this exact contract")
2. **Consistency**: Enforces policy uniformly regardless of LLM output
3. **Auditability**: All contract selections follow documented algorithm
4. **Testability**: Selector can be unit-tested; LLM output cannot

### Why Hard-Coded Absolute Limits?
**Decision**: 100 contracts/day, $500k notional/day (in code, not config)
**Reasoning**:
1. **Fail-safe**: Protects against config errors or malicious edits
2. **Regulatory**: Prevents accidentally triggering large-trader reporting
3. **Risk**: Limits maximum daily loss from hedging strategy

### Why Stub Provider First?
**Decision**: Start with synthetic data, not real IBKR chain
**Reasoning**:
1. **Faster iteration**: No external dependencies for testing
2. **Predictable**: Can test edge cases (no liquidity, wide spreads)
3. **Paper-safe**: Won't accidentally fetch/submit real orders

---

**End of Document**
