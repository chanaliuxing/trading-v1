# Production Deployment (8.md) - Summary

## Overview
Document 8.md describes the final steps to move from "research-grade" to "production-ready" trading system. It covers portfolio overlay risk management, reproducibility/audit metadata, shadow trading, monitoring, and production infrastructure.

## Production Readiness Assessment

### Current State (After 1-7.md Implementation)
**Research/Pre-Production Grade** with:
- ✅ Vectorized backtest engine with multi-symbol support
- ✅ Walk-forward cross-validation with purged gaps
- ✅ LLM/ML cooperative optimization (Chain-of-Alpha)
- ✅ Bayesian optimization (Optuna)
- ✅ Robust metrics (PSR/DSR/PBO)
- ✅ Data integrity guards (leakage_guard)
- ✅ Parameter safety (grid_guard)
- ✅ Extended metrics (Sortino/Omega/Calmar)

### Missing for Production (8.md Additions)
- ⚠️ Portfolio-level risk constraints (position limits, gross/net exposure)
- ⚠️ Reproducibility metadata (git/config tracking, seeds)
- ⚠️ Shadow trading engine
- ⚠️ SLO monitoring & alerting
- ⚠️ Kill-switch & runbook
- ⚠️ Secrets management
- ⚠️ State persistence & recovery

## Key Components from Document

### 1. Portfolio Overlay (Risk Management Layer)

**Purpose**: Hard constraints on portfolio-level risk, independent of strategy logic.

**File**: `portfolio_overlay.py` (documented but not implemented)

**Key Features**:

```python
@dataclass
class OverlayConfig:
    vol_target_annual: float = 0.15       # 15% annual volatility target
    vol_window: int = 390                 # ~1 day of 1m bars
    max_position_pct: float = 0.08        # 8% max single position
    max_gross: float = 2.0                # 2.0x max gross exposure
    max_net: float = 0.5                  # 0.5x max net exposure
    max_drawdown: float = 0.35            # 35% circuit breaker
    turnover_bps_budget: float = 30.0     # Soft turnover limit
    vol_floor: float = 0.05               # Min vol estimate (prevent over-leverage)
    vol_cap: float = 0.8                  # Max vol estimate (prevent de-risking)
```

**Functionality**:
1. **Volatility Targeting**: Dynamically adjust leverage to maintain target vol
2. **Position Limits**: Binary search to find max qty meeting constraints
3. **Drawdown Circuit Breaker**: Halt all trading if DD > threshold
4. **Exposure Constraints**: Gross (sum |positions|) and Net (sum positions) limits

**Integration Point**: `SimAdapter.submit_order()` calls `overlay.limit_order()` before execution

**Benefits**:
- Prevents strategy from taking excessive risk
- Protects against LLM/ML hallucinations proposing dangerous parameters
- Provides institutional-grade risk management layer
- Works in both backtest and live trading

### 2. Reproducibility & Audit Metadata

**Purpose**: Enable exact reproduction of any backtest run for compliance/debugging.

**Metadata Fields** (documented, implementation requires ClickHouse):
```python
meta = {
    "git_commit": os.getenv("GIT_COMMIT", "NA"),           # Code version
    "config_sha256": hashlib.sha256(config_bytes).hexdigest(),  # Config hash
    "data_snapshot": os.getenv("DATA_SNAPSHOT", "NA"),     # Data version
    "seed": int(os.getenv("GLOBAL_SEED", "42"))            # Random seed
}
```

**ClickHouse Schema Enhancement**:
```sql
ALTER TABLE alpha_runs
  ADD COLUMN IF NOT EXISTS git_commit String AFTER stage,
  ADD COLUMN IF NOT EXISTS config_sha256 String AFTER git_commit,
  ADD COLUMN IF NOT EXISTS data_snapshot String AFTER config_sha256,
  ADD COLUMN IF NOT EXISTS seed UInt32 AFTER data_snapshot;
```

**Workflow**:
1. Compute metadata at start of alpha_loop run
2. Set random seeds: `random.seed(seed); np.random.seed(seed)`
3. Write metadata to `manifest.json` in each generation directory
4. Insert metadata into ClickHouse for tracking

**Benefits**:
- Bit-for-bit reproducibility of any historical run
- Compliance/audit trail for institutional deployment
- Debugging aid (identify which code/config/data caused issue)
- Enables A/B testing of code changes

### 3. Shadow Trading Engine

**Purpose**: Run strategy in production with simulated execution before going live.

**File**: `paper_engine.py` (documented but not implemented)

**Architecture**:
```
Data Feed (CSV/Parquet/Real-time)
    ↓
SimAdapter (same as backtest)
    ↓
PortfolioOverlay (risk constraints)
    ↓
Strategy Callbacks
    ↓
Simulated Fills
    ↓
ClickHouse (live_runs, live_orders, live_nav)
```

**Key Features**:
- **Same Stack**: Uses identical SimAdapter/PortfolioOverlay as backtest
- **Circuit Breaker**: Halts on drawdown or minimum NAV breach
- **Alerting**: Webhook integration for Slack/PagerDuty
- **ClickHouse Sink**: Real-time metrics for Grafana dashboards
- **Signal Handling**: Graceful shutdown on SIGINT/SIGTERM

**Usage**:
```bash
python paper_engine.py \
  --config alpha_config_example.json \
  --feed csv \
  --feed-path data/live_feed.csv
```

**Benefits**:
- Test strategy in production environment without risking capital
- Identify data feed issues, latency problems, execution bugs
- Build confidence before enabling real trading
- Compare shadow vs real execution for validation

### 4. SLO Monitoring & Alerting

**Purpose**: Detect and alert on production anomalies.

**File**: `monitoring/slo.py` (documented but not implemented)

**Tracked Metrics**:
```python
@dataclass
class SLOThresholds:
    max_data_lag_ms: int = 5_000              # Data latency
    max_order_error_rate: float = 0.02        # Order failure rate
    max_shadow_real_diff_bps: float = 5.0     # Shadow vs real slippage
    window_sec: int = 300                     # 5-minute rolling window
```

**Functionality**:
1. **Data Latency Tracking**: `record_data_point_latency(market_ts, recv_ts)`
2. **Order Success Rate**: `record_order_attempt()` / `record_order_failure()`
3. **Slippage Monitoring**: `record_slippage_bps(ref_px, fill_px, side)`
4. **Shadow vs Real Diff**: `record_shadow_real_diff_bps(shadow_fill, real_fill)`
5. **Alert Generation**: Trigger webhooks when thresholds breached
6. **ClickHouse Export**: `flush_to_clickhouse()` for Grafana visualization

**ClickHouse Tables**:
```sql
CREATE TABLE live_metrics (
  run_id String,
  ts_ms UInt64,
  data_lag_ms_avg Float64,
  order_error_rate Float64,
  slippage_bps_avg Float64,
  shadow_real_diff_bps_avg Float64,
  window_sec UInt32
) ENGINE = MergeTree ORDER BY (run_id, ts_ms);

CREATE TABLE exec_compare (
  run_id String,
  ts_ms UInt64,
  symbol String,
  side LowCardinality(String),
  qty Int32,
  shadow_px Float64,
  real_px Float64,
  diff_bps Float64
) ENGINE = MergeTree ORDER BY (run_id, ts_ms);
```

**Benefits**:
- Early detection of data feed degradation
- Catch execution quality issues before major losses
- Quantify shadow vs real execution differences
- Provide metrics for SLA enforcement

### 5. Kill-Switch & Runbook

**Purpose**: Emergency shutdown capability with documented procedures.

**File**: `ops/killswitch.py` (documented but not implemented)

**Implementation**:
```python
DEFAULT_PATH = "/tmp/alpha_kill.flag"

def enable(path=DEFAULT_PATH, reason="manual"):
    # Write flag file with timestamp and reason
    Path(path).write_text(json.dumps({
        "enabled": True,
        "reason": reason,
        "ts": int(time.time())
    }))

def active(path=DEFAULT_PATH) -> tuple[bool, str]:
    # Check if flag file exists
    if Path(path).exists():
        data = json.loads(Path(path).read_text())
        return (data.get("enabled"), data.get("reason"))
    return (False, "")
```

**Runbook** (`RUNBOOK.md`):
```markdown
## Emergency Shutdown
1. Trigger: `python -c "from ops.killswitch import enable; enable(reason='SLO breach')"`
2. Verify: Check Grafana for halted=1 or zero order volume
3. Notify: Alert team via Slack/PagerDuty with reason/impact/ETA

## Recovery
1. Fix root cause
2. Disable: `python -c "from ops.killswitch import disable"`
3. Monitor: 30 minutes observation before scaling up

## Health Checks
- Data latency < threshold
- Order error rate < threshold
- Shadow vs real diff < threshold
- ClickHouse writes working (last 5 min)
- Portfolio drawdown < circuit breaker
```

**Benefits**:
- Instant halt capability without code changes
- Documented procedures reduce incident response time
- Reason tracking for post-mortem analysis
- File-based approach works even if services down

### 6. Secrets Management

**Purpose**: Secure storage and access control for credentials.

**File**: `ops/secrets.py` (documented but not implemented)

**Implementation**:
```python
def get_secret(name: str, vault_path=None, required=True) -> str:
    ALLOW = {
        "LLM_API_KEY", "CLICKHOUSE_PASSWORD",
        "BROKER_KEY", "BROKER_SECRET", "WEBHOOK_TOKEN"
    }
    if name not in ALLOW:
        raise SecretError(f"Secret {name} not allowed")

    # Priority: env var > vault.json
    v = os.getenv(name)
    if v: return v

    vault = json.load(open(vault_path or "/etc/alpha/vault.json"))
    return vault.get(name) or ("" if not required else raise SecretError)
```

**Best Practices**:
- Whitelist approach: Only allowed secrets can be accessed
- Never log or print secret values
- Environment variables take precedence over vault file
- Vault file should be readable only by service user

**Benefits**:
- Prevents accidental credential exposure
- Centralized secret management
- Audit trail of secret access
- Compatible with enterprise secret managers

### 7. State Persistence & Recovery

**Purpose**: Survive process crashes without position/cash loss.

**File**: `persistence/state_store.py` (documented but not implemented)

**Implementation**:
```python
class StateStore:
    def save(self, cash, positions, ledger_tail):
        snap = {
            "ts": int(time.time()*1000),
            "cash": float(cash),
            "positions": positions,
            "ledger_tail": ledger_tail[-1000:]
        }
        # Atomic write: tmp file + rename
        tmp.write_text(json.dumps(snap))
        tmp.replace(self.path)

    def load(self) -> dict:
        return json.load(open(self.path))
```

**Checkpoint Strategy**:
- Write every 10 seconds during paper trading
- Write every 1 second during live trading
- Atomic file writes prevent corruption
- Store last 1000 ledger entries for debugging

**Recovery Flow**:
1. On startup, check for existing state file
2. If found, load cash/positions/ledger
3. Resume from last checkpoint
4. Log recovery event for audit

**Benefits**:
- No position loss on crash/restart
- Fast recovery (seconds not hours)
- Debugging aid (ledger history)
- Compliance requirement for production trading

### 8. Dual Router & Reconciliation

**Purpose**: Gradual transition from shadow to live trading with monitoring.

**Files**: `broker/adapter.py`, `broker/router.py`, `recon/reconcile.py` (documented but not implemented)

**Routing Modes**:
```python
class DualRouter:
    # mode="shadow": All orders to SimAdapter, no real broker
    # mode="canary": X% to real broker, rest to SimAdapter
    # mode="prod": All orders to real broker

    def route(self, order, ref_px):
        if mode == "prod":
            return real.place_order(...)
        elif mode == "canary":
            q_real = int(qty * canary_ratio)
            q_shadow = qty - q_real
            return real.place_order(q_real) + sim.submit_order(q_shadow)
        else:  # shadow
            return sim.submit_order(order)
```

**Reconciliation**:
```python
def realtime_compare_and_log(shadow_fills, real_fills):
    # Match fills by (symbol, side, qty)
    # Calculate price difference in bps
    # Log to ClickHouse exec_compare table
    # Alert if diff > threshold

def eod_reconcile_positions(internal_pos, broker_pos):
    # Compare end-of-day positions
    # Return dict of discrepancies
    # Trigger manual investigation if needed
```

**Canary Workflow**:
1. Start with mode="shadow" for 1-2 weeks
2. If stable, switch to mode="canary" with canary_ratio=0.05 (5%)
3. Monitor shadow vs real fills, increase ratio gradually
4. When diff consistently <5bps, move to mode="prod"
5. Keep shadow running in parallel for monitoring

**Benefits**:
- Risk-controlled transition to live trading
- Quantify execution quality differences
- Early detection of broker API issues
- Maintain shadow execution for validation

## Production Checklist

### Implemented (from 1-7.md)
- [x] Backtest engine with multi-symbol support
- [x] Walk-forward cross-validation
- [x] LLM/ML/Optuna optimization
- [x] Robust metrics (PSR/DSR/PBO)
- [x] Data leakage prevention
- [x] Parameter constraint engine
- [x] Extended performance metrics

### Documented but Not Implemented (8.md)
- [ ] Portfolio overlay risk management
- [ ] Reproducibility metadata (git/config/seed tracking)
- [ ] Shadow trading engine
- [ ] SLO monitoring & alerting
- [ ] Kill-switch & runbook
- [ ] Secrets management
- [ ] State persistence
- [ ] Dual router & reconciliation

### Future Enhancements (8.md mentions)
- [ ] Real broker adapter (IBKR/Alpaca/Tradier)
- [ ] Prometheus metrics exporter
- [ ] Automated regression testing in CI/CD
- [ ] Multi-region deployment
- [ ] Real-time dashboard (Grafana)
- [ ] Automated alerting (PagerDuty/Slack)

## Implementation Priority

Based on 8.md, recommended implementation order:

### P0 (Immediate - Blocking for Canary)
1. **Portfolio Overlay**: Implement `portfolio_overlay.py` and integrate into `SimAdapter`
2. **Reproducibility**: Add metadata tracking to `alpha_loop.py`
3. **Shadow Engine**: Implement `paper_engine.py` for production validation

### P1 (Short-term - Blocking for Production)
1. **SLO Monitoring**: Implement `monitoring/slo.py` and ClickHouse tables
2. **Kill-Switch**: Implement `ops/killswitch.py` and create `RUNBOOK.md`
3. **Secrets**: Implement `ops/secrets.py` for credential management

### P2 (Medium-term - Production Hardening)
1. **State Persistence**: Implement `persistence/state_store.py`
2. **Dual Router**: Implement `broker/router.py` for canary deployment
3. **Reconciliation**: Implement `recon/reconcile.py` for position validation

### P3 (Long-term - Scale & Operations)
1. **Broker Adapter**: Integrate real broker SDK
2. **CI/CD**: Automated regression tests
3. **Observability**: Prometheus/Grafana dashboards
4. **Multi-region**: Deployment automation

## Configuration Example

Example production config incorporating 8.md concepts:

```json
{
  "hpo": { ... },
  "guard": { ... },
  "overlay": {
    "enable": true,
    "vol_target_annual": 0.15,
    "max_position_pct": 0.08,
    "max_gross": 2.0,
    "max_net": 0.5,
    "max_drawdown": 0.35
  },
  "route": {
    "mode": "shadow",
    "canary_ratio": 0.05
  },
  "slo": {
    "max_data_lag_ms": 5000,
    "max_order_error_rate": 0.02,
    "max_shadow_real_diff_bps": 5.0,
    "window_sec": 300
  },
  "alerts": {
    "enabled": true,
    "webhooks": ["https://hooks.slack.com/services/..."]
  },
  "sink_clickhouse": {
    "enabled": true,
    "host": "localhost",
    "port": 8123,
    "database": "trading"
  }
}
```

## Deployment Workflow

### Phase 1: Shadow Trading (Weeks 1-2)
```bash
# Terminal 1: Start shadow engine
python paper_engine.py \
  --config prod_config.json \
  --feed csv \
  --feed-path data/live_feed.csv

# Terminal 2: Monitor Grafana
open http://localhost:3000/d/shadow-trading

# Verify:
- Data latency < 5s
- No order failures
- Drawdown within limits
- NAV trend matches expectations
```

### Phase 2: Canary Trading (Weeks 3-6)
```bash
# Update config: route.mode = "canary", canary_ratio = 0.05
# Enable real broker adapter
# Monitor shadow vs real fills

# Week 3: 5% real
# Week 4: 10% real (if stable)
# Week 5: 25% real (if stable)
# Week 6: 50% real (if stable)

# Each week, verify:
- Shadow vs real diff < 5bps
- No order failures > 2%
- Positions reconcile daily
```

### Phase 3: Production (Week 7+)
```bash
# Update config: route.mode = "prod"
# Keep shadow running for monitoring
# Enable automated alerts
# Daily reconciliation

# Ongoing:
- Monitor SLO dashboards
- Weekly strategy review
- Monthly performance attribution
- Quarterly compliance audit
```

## Testing Approach

### Regression Tests (from document)

```python
# tests/test_regression.py
def test_oos_metrics_regression():
    runner = WalkForwardHPO(CFG)
    res = runner.run()
    oos = res["oos_summary"]

    assert oos["oos_avg_sharpe"] >= 0.80
    assert oos["oos_avg_max_drawdown"] <= 0.35
    assert oos["oos_trades"] >= 30
```

### CI/CD Integration

```yaml
# .github/workflows/ci.yml
name: backtest-regression
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: pytest -q
```

## Monitoring Dashboards

### Grafana Panels (Recommended)

1. **Live Trading Health**:
   - NAV time series
   - Drawdown gauge
   - Position heatmap
   - Order volume (shadow vs real)

2. **SLO Metrics**:
   - Data latency trend
   - Order error rate
   - Shadow vs real diff distribution
   - Alert history

3. **Strategy Performance**:
   - Sharpe ratio (rolling 30d)
   - Win rate
   - Profit factor
   - Trade count

4. **Risk Metrics**:
   - Gross exposure
   - Net exposure
   - Largest position
   - Turnover rate

## Summary

Document 8.md provides a comprehensive production deployment framework:

**Core Philosophy**:
- Start with shadow trading (zero risk)
- Gradual canary rollout (controlled risk)
- Robust monitoring (early detection)
- Kill-switch capability (instant response)

**Key Additions**:
1. **Portfolio Overlay**: Hard risk limits independent of strategy
2. **Reproducibility**: Complete audit trail
3. **Shadow Trading**: Production validation without capital risk
4. **SLO Monitoring**: Continuous health checks
5. **Operational Tools**: Kill-switch, secrets, persistence

**Production Gap**:
The system is currently **research-grade** (documents 1-7 implemented). Implementing document 8 would make it **production-ready**. However, production deployment requires:
- Infrastructure (ClickHouse, Grafana, broker connectivity)
- Operations team (on-call, runbooks, incident response)
- Compliance review (depending on jurisdiction)

**Recommendation**:
Implement in phases as described in deployment workflow. Start with P0 items (overlay, metadata, shadow engine) to enable shadow trading. Add P1 items for canary deployment. P2-P3 are for scaling and hardening.

**Files Documented (not implemented)**:
- `portfolio_overlay.py` (~160 lines)
- `paper_engine.py` (~240 lines)
- `monitoring/slo.py` (~120 lines)
- `ops/killswitch.py` (~20 lines)
- `ops/secrets.py` (~35 lines)
- `persistence/state_store.py` (~25 lines)
- `broker/adapter.py` (~20 lines)
- `broker/router.py` (~30 lines)
- `recon/reconcile.py` (~80 lines)
- `RUNBOOK.md` (documentation)

**Total**: ~730 lines for full production infrastructure, plus operational procedures and monitoring setup.

**Status**: System is **ready for shadow trading** with manual implementation of P0 components. Full production deployment requires institutional infrastructure and operations support.
