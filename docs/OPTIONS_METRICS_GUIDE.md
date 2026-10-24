# Options Hedging Prometheus Metrics Guide

**Date**: 2025-11-02
**Status**: ✅ Implemented
**Namespace**: `ai_hedging_*`

---

## Overview

The options hedging system exports comprehensive Prometheus metrics for monitoring selection quality, execution status, position risk, and operational health. These metrics integrate with the existing AI trading system metrics infrastructure.

---

## Available Metrics

### 1. Selection Metrics

**`ai_hedging_selection_total{mode, status}`** (Counter)
- Total number of hedge contract selections
- Labels:
  - `mode`: `options` or `stock`
  - `status`: `success`, `no_need`, `rejected`, `error`
- Use case: Monitor selection attempts and success rate

**`ai_hedging_selection_duration_seconds{mode}`** (Histogram)
- Duration of contract selection process
- Labels: `mode` (options/stock)
- Buckets: `[0.1, 0.5, 1, 2, 5, 10, 30]` seconds
- Use case: Detect slow IBKR API calls or selector performance issues

**`ai_hedging_contract_quality{metric}`** (Histogram)
- Quality metrics of selected contracts
- Labels:
  - `metric`: `spread_bps`, `open_interest`, `delta_error`
- Buckets: `[0, 5, 10, 20, 50, 100, 500, 1000, 5000, 10000]`
- Use case: Monitor execution quality (tight spreads, high liquidity, accurate delta)

Example PromQL:
```promql
# Selection success rate
rate(ai_hedging_selection_total{status="success"}[5m]) /
rate(ai_hedging_selection_total[5m])

# p95 selection latency
histogram_quantile(0.95, rate(ai_hedging_selection_duration_seconds_bucket[5m]))

# Average spread of selected contracts
rate(ai_hedging_contract_quality_sum{metric="spread_bps"}[1h]) /
rate(ai_hedging_contract_quality_count{metric="spread_bps"}[1h])
```

---

### 2. Execution Metrics

**`ai_hedging_execution_total{mode, status}`** (Counter)
- Total number of hedge executions
- Labels:
  - `mode`: `options`, `stock`, `options_real`, `overlay`
  - `status`: `submitted`, `rejected`, `dry_run`, `error`
- Use case: Track execution attempts and failures

**`ai_hedging_rejection_total{reason}`** (Counter)
- Total number of hedge rejections by reason
- Labels:
  - `reason`: `daily_limit`, `spread_too_wide`, `low_oi`, `validation_failed`, `no_contracts`
- Use case: Identify why hedges are being rejected

Example PromQL:
```promql
# Execution success rate
rate(ai_hedging_execution_total{status="submitted"}[5m]) /
rate(ai_hedging_execution_total[5m])

# Top rejection reasons
topk(3, rate(ai_hedging_rejection_total[1h]))
```

---

### 3. Daily Limit Metrics

**`ai_hedging_daily_contracts`** (Gauge)
- Number of option contracts executed today
- Use case: Monitor against daily limit (e.g., 50 contracts)

**`ai_hedging_daily_notional_usd`** (Gauge)
- Total notional USD executed today
- Use case: Monitor against daily notional limit (e.g., $100k)

Example PromQL:
```promql
# Utilization against limit (assuming 50 contract limit)
ai_hedging_daily_contracts / 50

# Alert if approaching limit
ai_hedging_daily_contracts > 45
```

---

### 4. Position Metrics

**`ai_hedging_positions_open{underlying}`** (Gauge)
- Number of open option positions by underlying
- Labels: `underlying` (e.g., SPY, QQQ)
- Use case: Monitor open position count

**`ai_hedging_positions_expiring_soon`** (Gauge)
- Number of positions expiring within 3 days
- Use case: Alert for manual roll or close decisions

**`ai_hedging_portfolio_greeks{greek}`** (Gauge)
- Portfolio-level Greek exposures
- Labels:
  - `greek`: `delta`, `gamma`, `vega`, `theta`
- Use case: Monitor total portfolio sensitivity

**`ai_hedging_portfolio_pnl_usd`** (Gauge)
- Unrealized P&L from option positions (USD)
- Use case: Track hedge cost/profit

Example PromQL:
```promql
# Total open positions across all underlyings
sum(ai_hedging_positions_open)

# Portfolio delta exposure
ai_hedging_portfolio_greeks{greek="delta"}

# Alert on expiring positions
ai_hedging_positions_expiring_soon > 0
```

---

## Metrics Collection Flow

### Event-Based Metrics (Immediate)

1. **Selection Event** → `hedging_events.py:log_selection()`
   - Increments `ai_hedging_selection_total`
   - Records `ai_hedging_selection_duration_seconds`
   - Records `ai_hedging_contract_quality` (spread, OI, delta error)

2. **Execution Event** → `hedging_events.py:log_execution()`
   - Increments `ai_hedging_execution_total`

3. **Rejection Event** → `hedging_events.py:log_rejection()`
   - Increments `ai_hedging_rejection_total`

### Position-Based Metrics (Periodic)

**Updated by**: `scripts/ai_trader/risk/update_hedging_metrics.py`

**Schedule**: Every 5 minutes (recommended)

**Process**:
1. Load positions from `runs/ai_trader/options_positions.json`
2. Optionally fetch latest market data from IBKR
3. Update position counts, Greeks, P&L gauges
4. Read today's events to calculate daily contracts/notional

**Run Command**:
```bash
# Single run
python scripts/ai_trader/risk/update_hedging_metrics.py --runs-dir runs/ai_trader

# Continuous (every 5 min)
python scripts/ai_trader/risk/update_hedging_metrics.py --runs-dir runs/ai_trader --loop
```

**Systemd Service** (Production):
```ini
# /etc/systemd/system/hedging-metrics.service
[Unit]
Description=Options Hedging Metrics Updater
After=network.target

[Service]
Type=simple
User=trading
WorkingDirectory=.
ExecStart=./venv/bin/python scripts/ai_trader/risk/update_hedging_metrics.py --loop
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Integration with Existing Metrics

The hedging metrics are exported alongside existing AI trading system metrics in `libs/metrics.py`. All metrics are available at the standard Prometheus endpoint (typically `:8000/metrics`).

**Existing Metrics Namespaces**:
- `ai_brain_*` - Brain call metrics
- `ai_nightly_*` - Nightly task metrics
- `ai_strategy_*` - Strategy decision metrics
- `ai_gate_*` - Gate check metrics
- `ai_hedging_*` - **Options hedging metrics (NEW)**

---

## Alerting Rules (Recommended)

### Critical Alerts

```yaml
groups:
  - name: hedging_critical
    interval: 1m
    rules:
      - alert: HedgingDailyLimitApproaching
        expr: ai_hedging_daily_contracts > 45
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Options hedging approaching daily limit"
          description: "{{ $value }} contracts executed today (limit: 50)"

      - alert: HedgingDailyLimitExceeded
        expr: ai_hedging_daily_contracts >= 50
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Options hedging daily limit EXCEEDED"
          description: "{{ $value }} contracts executed (limit: 50)"

      - alert: HedgingPositionsExpiringSoon
        expr: ai_hedging_positions_expiring_soon > 0
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "Options positions expiring within 3 days"
          description: "{{ $value }} positions require manual review for roll/close"

      - alert: HedgingExecutionFailures
        expr: |
          rate(ai_hedging_execution_total{status="error"}[5m]) > 0.5
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High hedge execution failure rate"
          description: "{{ $value }} executions/sec failing"
```

### Quality Alerts

```yaml
  - name: hedging_quality
    interval: 5m
    rules:
      - alert: HedgingWideSpread
        expr: |
          rate(ai_hedging_contract_quality_sum{metric="spread_bps"}[1h]) /
          rate(ai_hedging_contract_quality_count{metric="spread_bps"}[1h]) > 50
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Selected contracts have wide spreads"
          description: "Average spread: {{ $value }} bps (threshold: 50bps)"

      - alert: HedgingLowOI
        expr: |
          rate(ai_hedging_contract_quality_sum{metric="open_interest"}[1h]) /
          rate(ai_hedging_contract_quality_count{metric="open_interest"}[1h]) < 5000
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Selected contracts have low open interest"
          description: "Average OI: {{ $value }} (threshold: 5000)"

      - alert: HedgingSlowSelection
        expr: |
          histogram_quantile(0.95,
            rate(ai_hedging_selection_duration_seconds_bucket[5m])
          ) > 10
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Hedge contract selection is slow"
          description: "p95 latency: {{ $value }}s (threshold: 10s)"
```

---

## Grafana Dashboard Queries

### Selection Performance Panel
```promql
# Selection success rate (%)
100 * (
  rate(ai_hedging_selection_total{status="success"}[5m]) /
  rate(ai_hedging_selection_total[5m])
)

# Selection rate (selections/min)
rate(ai_hedging_selection_total[5m]) * 60

# p50, p95, p99 latency
histogram_quantile(0.50, rate(ai_hedging_selection_duration_seconds_bucket[5m]))
histogram_quantile(0.95, rate(ai_hedging_selection_duration_seconds_bucket[5m]))
histogram_quantile(0.99, rate(ai_hedging_selection_duration_seconds_bucket[5m]))
```

### Execution Status Panel
```promql
# Execution by status (stacked area)
rate(ai_hedging_execution_total[5m]) by (status)

# Rejection breakdown (pie chart)
sum(rate(ai_hedging_rejection_total[1h])) by (reason)
```

### Daily Limits Panel
```promql
# Contracts executed today
ai_hedging_daily_contracts

# Contracts remaining
50 - ai_hedging_daily_contracts

# Notional utilization (%)
100 * (ai_hedging_daily_notional_usd / 100000)
```

### Position Risk Panel
```promql
# Open positions by underlying
ai_hedging_positions_open

# Portfolio Greeks
ai_hedging_portfolio_greeks by (greek)

# Unrealized P&L
ai_hedging_portfolio_pnl_usd

# Expiring positions
ai_hedging_positions_expiring_soon
```

### Contract Quality Panel
```promql
# Average spread over time
rate(ai_hedging_contract_quality_sum{metric="spread_bps"}[5m]) /
rate(ai_hedging_contract_quality_count{metric="spread_bps"}[5m])

# Average OI over time
rate(ai_hedging_contract_quality_sum{metric="open_interest"}[5m]) /
rate(ai_hedging_contract_quality_count{metric="open_interest"}[5m])

# Delta error distribution (heatmap)
rate(ai_hedging_contract_quality_bucket{metric="delta_error"}[5m])
```

---

## Testing Metrics Emission

### 1. Check Metrics Availability

```bash
# Start metrics updater
python scripts/ai_trader/risk/update_hedging_metrics.py --runs-dir runs/ai_trader

# Check Prometheus endpoint (if running)
curl localhost:8000/metrics | grep ai_hedging
```

### 2. Trigger Test Events

```python
from scripts.ai_trader.risk.hedging_events import HedgingEventsLogger

logger = HedgingEventsLogger('runs/ai_trader')

# Test selection metric
logger.log_selection(
    success=True,
    mode='options',
    details={
        'contract': {
            'occ_symbol': 'SPY   251109P00450000',
            'spread_bps': 12.5,
            'open_interest': 8500,
            'delta': -0.41,
        },
        'delta_target': 0.40,
    },
    duration_seconds=2.3,
)

# Test rejection metric
logger.log_rejection(
    reason='daily_limit',
    mode='options',
    details={'contracts': 50, 'limit': 50},
)

# Verify metrics incremented
# Check Prometheus endpoint or query via Grafana
```

### 3. Verify Position Metrics

```python
from scripts.ai_trader.risk.options_positions import get_position_tracker

tracker = get_position_tracker('runs/ai_trader')

# Add test position
tracker.add_position(
    occ_symbol='SPY   251116P00450000',
    quantity=5,
    entry_price=5.20,
    delta=-0.40,
)

# Update metrics
tracker.update_metrics()

# Check: ai_hedging_positions_open{underlying="SPY"} should be 5
# Check: ai_hedging_portfolio_greeks{greek="delta"} should be -200 (5 * 100 * -0.40)
```

---

## Troubleshooting

### Metrics Not Appearing

**Symptom**: Metrics not visible in Prometheus scrape

**Checks**:
1. Verify `prometheus_client` is installed:
   ```bash
   pip list | grep prometheus-client
   ```
2. Check metrics are enabled in code:
   ```python
   from libs.metrics import is_enabled
   print(is_enabled())  # Should be True
   ```
3. Verify Prometheus scrape config includes hedging metrics endpoint
4. Check no-op fallback isn't active (ImportError in logs)

### Stale Position Metrics

**Symptom**: Position gauges not updating

**Checks**:
1. Verify `update_hedging_metrics.py` is running:
   ```bash
   ps aux | grep update_hedging_metrics
   ```
2. Check for errors in script output
3. Verify `options_positions.json` exists and is readable
4. Check IBKR connection if using live data provider

### Metrics Spikes/Anomalies

**Symptom**: Sudden spikes in rejection counters

**Investigation**:
1. Check `hedging_events.jsonl` for rejection details:
   ```bash
   cat runs/ai_trader/$(date +%Y-%m-%d)/hedging_events.jsonl | \
     jq 'select(.event_type=="rejection")'
   ```
2. Correlate with market events (open/close, volatility)
3. Check IBKR connection status
4. Verify config changes didn't tighten filters too much

---

## Production Deployment Checklist

- [ ] Metrics library installed (`prometheus_client`)
- [ ] Metrics exporter endpoint running (port 8000 or configured)
- [ ] `update_hedging_metrics.py` running as systemd service
- [ ] Prometheus scraping hedging metrics (check `/targets`)
- [ ] Alerting rules deployed (`hedging_critical`, `hedging_quality`)
- [ ] Grafana dashboard created with recommended panels
- [ ] Test alerts trigger correctly (manual position creation)
- [ ] Runbook updated with metric interpretation guide

---

## Future Enhancements

**Planned for Phase 3**:
- Fill rate metrics (order submitted → filled time)
- Slippage distribution (fill price vs limit price)
- Roll effectiveness metrics (P&L on rolled positions)
- IBKR API call latency breakdown
- Position concentration metrics (% in single underlying)

---

**Last Updated**: 2025-11-02
**Maintainer**: Trading Team
**Related Docs**: `OPTIONS_HEDGING_IMPLEMENTATION.md`, `OPTIONS_PHASE2_DEPLOYMENT.md`
