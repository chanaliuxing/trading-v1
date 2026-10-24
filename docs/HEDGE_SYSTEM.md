# Hedge System Documentation

## Overview

The hedging system provides automated portfolio risk management through dynamic hedging strategies. It integrates with the AI Brain control system to automatically execute protective hedges based on confidence levels, volatility conditions, and risk exposure.

**Status**: Production-ready (dry-run mode). Live execution requires IB integration completion.

---

## Architecture

### Components

1. **`scripts/options/auto_hedge.py`**: Core hedge execution engine
   - Generates hedge orders (options or stock shorts)
   - Validates cost constraints
   - Executes hedges via broker (pending IB integration)
   - Audit logging and metrics

2. **`llm_trader/plan_types.py`**: Schema definitions
   - `HedgeIntent`: Hedge specification from control plan
   - Integrated into `DailyPlan` schema

3. **`scripts/ai_trader/controllers/llm_trader_controller.py`**: Intent consumption
   - Parses `hedge_intent` from control plans
   - Executes `auto_hedge.py` script
   - Implements fail-open safety mode

4. **Monitoring & Observability**:
   - `scripts/ai_trader/monitoring_dashboard.py`: Real-time hedge status
   - `scripts/ai_trader/health_snapshot.py`: Historical statistics
   - `libs/metrics.py`: Prometheus metrics

---

## Hedge Strategies

### 1. Protective Put (`options_put`)

**Description**: Buy OTM put options to protect against downside risk.

**Use Case**:
- Low confidence signals (conf < 0.6)
- High volatility environments (VIX > 25)
- Directional exposure with tail risk protection

**Parameters**:
- Strike: Typically 5% OTM (95% of current price)
- Expiry: ~30 days (next monthly expiration)
- Delta: ~0.30 (OTM put)

**Cost**: ~2% of notional (estimated)

**Example**:
```bash
python scripts/options/auto_hedge.py \
  --symbols TSLA,NVDA \
  --notional-pct 0.5 \
  --strategy options_put \
  --urgency medium
```

### 2. Collar (`options_collar`)

**Description**: Long put + short call to create a cost-effective hedge.

**Use Case**:
- Medium confidence with budget constraints
- Willing to cap upside for cheaper downside protection
- Cost-conscious hedging

**Parameters**:
- Put strike: 95% of current price
- Call strike: 105% of current price
- Net cost: ~0.5% of notional (put premium - call premium)

**Example**:
```bash
python scripts/options/auto_hedge.py \
  --symbols AAPL \
  --notional-pct 0.75 \
  --strategy options_collar \
  --urgency high \
  --max-cost-bps 50
```

### 3. Stock Short (`stock_short`)

**Description**: Short index ETF (SPY/QQQ) for delta hedge.

**Use Case**:
- Beta exposure reduction
- High correlation to index (corr > 0.8)
- No options liquidity available

**Parameters**:
- Hedge ratio: 1.0 (full delta)
- No expiry (equity position)

**Example**:
```bash
python scripts/options/auto_hedge.py \
  --symbols TSLA \
  --notional-pct 1.0 \
  --strategy stock_short \
  --instrument SPY
```

---

## Hedge Instruments

### Whitelisted Hedge Vehicles

| Instrument | Priority | Beta to SPY | Min Liquidity | Use Case |
|------------|----------|-------------|---------------|----------|
| SPY | 1 | 1.00 | 100,000 | Broad market hedge |
| QQQ | 2 | 1.15 | 80,000 | Tech/growth exposure |
| IWM | 3 | 0.95 | 50,000 | Small-cap exposure |
| DIA | 4 | 0.90 | 30,000 | Large-cap/value |

### Automatic Instrument Selection

The system selects the best hedge instrument based on:

1. **Correlation**: Choose instrument with highest correlation to portfolio symbols
2. **Liquidity**: Ensure minimum open interest and volume
3. **Priority**: SPY preferred for broad exposure

**Correlation Matrix** (default, should be updated from live data):

```python
SYMBOL_CORRELATIONS = {
    "TSLA": {"SPY": 0.65, "QQQ": 0.75, "IWM": 0.45},
    "NVDA": {"SPY": 0.70, "QQQ": 0.80, "IWM": 0.50},
    "AAPL": {"SPY": 0.75, "QQQ": 0.85, "IWM": 0.55},
}
```

**Example**: For NVDA positions, QQQ is automatically selected (correlation 0.80).

---

## Confidence-Based Sizing

The hedge system adjusts hedge size based on signal confidence:

| Confidence Range | Multiplier | Hedge Action |
|------------------|------------|--------------|
| < 0.6 (Low) | 1.0x | Full hedge (100% of notional_pct) |
| 0.6 - 0.8 (Medium) | 0.5x | Partial hedge (50% of notional_pct) |
| >= 0.8 (High) | 0.25x | Light hedge (25% of notional_pct) |

**Example**:

```python
# Plan with confidence-based sizing
hedge_intent = {
    "symbols": ["TSLA"],
    "notional_pct": 0.5,
    "confidence": 0.4,  # Low confidence
    # Effective hedge: 0.5 * 1.0 = 50% of position notional
}
```

---

## Fail-Open Safety Mode

When hedge execution fails, the system automatically adjusts the trading plan to reduce risk:

### Urgency Levels

1. **High Urgency**:
   - Enter `safe_mode = true`
   - Add restrictions: `["reduce_sizing", "slow_interval"]`
   - Increase all provider intervals to >= 600s
   - **Rationale**: Hedge critical for risk management, cannot trade safely without it

2. **Medium Urgency**:
   - Add restriction: `["reduce_sizing"]`
   - Keep normal intervals
   - **Rationale**: Reduce exposure but continue trading

3. **Low Urgency**:
   - Log warning, no plan adjustments
   - **Rationale**: Hedge is optional, proceed normally

### Example Failure Scenarios

```python
# Scenario 1: Cost exceeds threshold
hedge_intent = {
    "symbols": ["TSLA"],
    "notional_pct": 1.0,
    "urgency": "high",
    "max_cost_bps": 50,
}
# If hedge costs 150 bps → Execution fails
# System enters safe_mode and reduces sizing
```

```python
# Scenario 2: No positions to hedge
hedge_intent = {
    "symbols": ["UNKNOWN"],
    "notional_pct": 0.5,
}
# No positions → Hedge skipped (success=True, error="no_positions_to_hedge")
```

---

## Integration with Control Plan

### Schema

The Brain control plan includes `hedge_intent` field:

```python
{
  "session": "morning",
  "providers": [...],
  "llm_trader_flags": {...},
  "hedge_intent": {
    "symbols": ["TSLA", "NVDA"],
    "notional_pct": 0.5,           # 0.0-1.0
    "urgency": "medium",           # low/medium/high
    "instrument": "QQQ",           # Optional: SPY/QQQ/IWM/DIA
    "strategy": "options_put",     # options_put/options_collar/stock_short
    "confidence": 0.7,             # Optional: 0.0-1.0
    "max_cost_bps": 100            # Max hedge cost in bps
  }
}
```

### Brain Prompt Guidelines

The AI Brain MUST include `hedge_intent` when:
- Trading with confidence < 0.8
- VIX > 25 (high volatility)
- Opening new positions with uncertainty
- Prime Directive #3: "Hedge Enables Leverage"

**Example Prompt Addition**:

```
6) Hedge Intent MANDATORY: When trading with confidence<0.8 or during high
   volatility (VIX>25), you MUST include hedge_intent in your plan with
   appropriate sizing and urgency.
```

### Execution Flow

```
1. Brain generates plan with hedge_intent
   ↓
2. LLMTraderController.run_with_plan() parses hedge_intent
   ↓
3. Execute auto_hedge.py script (dry-run or live)
   ↓
4. If successful: Continue with normal trading
   If failed: Apply fail-open safety adjustments
   ↓
5. Write audit logs to hedge_audit.jsonl
   ↓
6. Update Prometheus metrics
```

---

## Monitoring & Observability

### Dashboard Metrics

Access via `streamlit run scripts/ai_trader/monitoring_dashboard.py`:

**🛡️ Hedge Execution Status**:
- Today's hedge trigger count
- Success rate (7-day)
- Total hedge cost (USD)
- Average hedge effectiveness
- Recent hedge history (last 10)
- Failed hedges with error details

### Health Snapshot

Run `python scripts/ai_trader/health_snapshot.py` to generate:

```json
{
  "hedge_execution": {
    "total_triggers": 15,
    "successful": 13,
    "failed": 2,
    "success_rate": 0.867,
    "total_cost_usd": 1250.75,
    "avg_effectiveness": 0.72,
    "by_strategy": {
      "options_put": 10,
      "options_collar": 3,
      "stock_short": 2
    },
    "failures_by_reason": {
      "cost_exceeded": 1,
      "timeout": 1
    }
  }
}
```

### Prometheus Metrics

```python
# Counter: Hedge executions
ai_hedge_executions_total{strategy="options_put", status="success", dry_run="true"}

# Histogram: Hedge cost
ai_hedge_cost_usd{strategy="options_put"}

# Gauge: Current hedge effectiveness
ai_hedge_effectiveness{strategy="options_put"}

# Counter: Failure reasons
ai_hedge_failure_reason_total{reason="cost_exceeded"}
```

### Audit Logs

All hedge executions are logged to `runs/ai_trader/YYYY-MM-DD/hedge_audit.jsonl`:

```json
{
  "ts": "2025-12-10T09:30:00-05:00",
  "intent": {
    "symbols": ["TSLA"],
    "notional_pct": 0.5,
    "urgency": "medium",
    "strategy": "options_put",
    "confidence": 0.7
  },
  "result": {
    "success": true,
    "orders_count": 1,
    "total_cost": 250.50,
    "hedge_effectiveness": 0.75,
    "dry_run": true
  },
  "orders": [
    {
      "instrument": "QQQ",
      "side": "buy",
      "qty": 2,
      "order_type": "mid",
      "symbol_type": "option",
      "occ_symbol": "QQQ   250120P00485000",
      "expiry": "2025-01-20",
      "strike": 485.0,
      "right": "P",
      "estimated_cost": 250.50,
      "hedge_ratio": 0.30
    }
  ]
}
```

---

## Testing

### Unit Tests

```bash
# Run all hedge tests
pytest tests/unit/test_auto_hedge.py -v

# Run specific test
pytest tests/unit/test_auto_hedge.py::TestAutoHedgeExecutor::test_execute_hedge_dry_run_success -v
```

**Coverage**:
- ✅ Hedge intent creation
- ✅ Instrument selection (correlation-based)
- ✅ Confidence-to-size multiplier
- ✅ Put/collar/short order generation
- ✅ Cost validation
- ✅ Audit log writing

### Integration Tests

```bash
# Run e2e hedge intent flow test
pytest tests/integration/test_hedge_intent_e2e.py -v
```

**Coverage**:
- ✅ Plan with hedge_intent execution
- ✅ Fail-open safety mode (high/medium/low urgency)
- ✅ Audit log creation
- ✅ Paper mode flag propagation
- ✅ Monitoring dashboard integration

### Manual Dry-Run Test

```bash
# Test protective put hedge
python scripts/options/auto_hedge.py \
  --symbols TSLA,NVDA \
  --notional-pct 0.5 \
  --urgency medium \
  --strategy options_put \
  --confidence 0.6

# Expected output:
# ================================================================================
# HEDGE EXECUTION RESULT
# ================================================================================
# Success: True
# Dry Run: True
# Orders Generated: 1
# Total Cost: $XXX.XX
# Hedge Effectiveness: 0.75
#
# Orders:
#   1. BUY 3 QQQ option
#      OCC: QQQ   250120P00485000
#      Expiry: 2025-01-20, Strike: $485.0, Right: P
#      Est. Cost: $XXX.XX
#      Hedge Ratio: 0.30
```

---

## Production Deployment

### Prerequisites

1. **Interactive Brokers Integration**:
   - [ ] Options trading permission enabled
   - [ ] Real-time options data subscription
   - [ ] Order submission API tested

2. **Data Requirements**:
   - [ ] Real-time correlation matrix (update SYMBOL_CORRELATIONS)
   - [ ] Options chain data feed
   - [ ] Bid-ask spread monitoring

3. **Risk Controls**:
   - [ ] Position size limits configured
   - [ ] Maximum hedge cost per day
   - [ ] Options approval workflow (if required)

### Enabling Live Execution

1. **Update auto_hedge.py**:

```python
# Remove --live flag comment in llm_trader_controller.py:107
cmd_parts.append("--live")  # TODO: Enable after IB integration
```

2. **Configure Risk Limits** (config.yml):

```yaml
risk_management:
  hedge:
    auto_execute: true
    max_daily_cost_usd: 5000
    max_cost_bps: 150
    required_approvals: []  # or ["risk_manager"] for manual approval
```

3. **Monitor First Week**:
   - Start with paper trading (--paper flag)
   - Review all audit logs daily
   - Validate cost estimates vs. actual fills
   - Check hedge effectiveness post-market

---

## Troubleshooting

### Issue: Hedge cost too high

**Symptom**: `Error: hedge_cost_too_high: XXX bps > YYY bps`

**Solutions**:
- Increase `max_cost_bps` in hedge_intent
- Use collar strategy instead of naked put
- Reduce `notional_pct`
- Check implied volatility (high IV → expensive options)

### Issue: No positions to hedge

**Symptom**: `success: True, error: "no_positions_to_hedge"`

**Cause**: Hedge intent executed before positions opened

**Solutions**:
- Delay hedge execution until after position entry
- Skip hedge if no positions exist (current behavior)

### Issue: Hedge execution timeout

**Symptom**: `Error: timeout`

**Solutions**:
- Check options data feed latency
- Increase timeout from 30s to 60s
- Verify IB connectivity

### Issue: Wrong hedge instrument selected

**Symptom**: Using SPY when QQQ is more correlated

**Solutions**:
- Update SYMBOL_CORRELATIONS with live rolling correlations
- Explicitly specify `instrument: "QQQ"` in hedge_intent
- Check correlation calculation logic

---

## Future Enhancements

### Phase 2: Advanced Strategies

- [ ] Dynamic delta hedging (rebalance as positions change)
- [ ] Volatility targeting (increase hedge when VIX spikes)
- [ ] Portfolio-level VaR hedging
- [ ] Credit spread hedges (for vol premium capture)

### Phase 3: ML-Based Optimization

- [ ] Learn optimal hedge timing from historical data
- [ ] Predict hedge effectiveness using regime models
- [ ] Backtestable hedge P&L attribution

### Phase 4: Multi-Asset Hedging

- [ ] Futures hedges (ES/NQ for lower cost)
- [ ] VIX futures for volatility hedging
- [ ] Currency hedges for international exposure

---

## References

- **Code**: `scripts/options/auto_hedge.py`
- **Schema**: `llm_trader/plan_types.py:HedgeIntent`
- **Controller**: `scripts/ai_trader/controllers/llm_trader_controller.py:_execute_hedge_intent`
- **Tests**: `tests/unit/test_auto_hedge.py`, `tests/integration/test_hedge_intent_e2e.py`
- **Monitoring**: `scripts/ai_trader/monitoring_dashboard.py` (line 182-275)
- **Metrics**: `libs/metrics.py` (line 291-318)

---

**Last Updated**: 2025-12-10
**Version**: 1.0.0
**Status**: Production-ready (dry-run mode)
