# Dashboard Upgrade Implementation Status

**Date:** 2025-10-20
**Status:** Consolidation In Progress 🚧

---

## Phase‑0 Reality Check (2025‑10‑20)

| Area | Result | Notes |
| --- | --- | --- |
| Prometheus Targets | ✅ `prometheus`, `trading-system` | Query: `curl -s http://localhost:9090/api/v1/targets` |
| Prometheus Metrics | ⚠️ `latency_*`, `service_up` only | No `container_*`, `kafka_*lag`, or `errors_total` present yet. |
| Loki Labels | ⚠️ Limited (`job`, `level`, `event`, `reason`, `strategy`, `symbol`, `filename`) | No `component`, `app`, or `service` labels available; latest range query returned empty, indicating idle system. |
| ClickHouse Tables | ✅ Full trading schema present | Verified via `SHOW TABLES FROM trading`; `pnl_day`, `gate_snapshots`, `order_funnel_daily`, market `bars_*` tables available. |
| PostgreSQL Metadata | ❌ Not verified | No direct psql access in current environment; must derive strategy status from logs/ClickHouse for now. |

Reality check snapshot captured in `doc/dashboard/PANEL_MATRIX.csv` and dashboard blueprints below.

---

## What's Been Implemented

## What's Been Implemented

### 1. ClickHouse Observability Schema ✅
**Location:** `schema/clickhouse/observability/observability_schema.sql`

Created 7 tables:
- `equity_pnl_snapshots` - Account P&L tracking (ReplacingMergeTree)
- `positions_snapshots` - Position snapshots (ReplacingMergeTree)
- `order_events` - Order lifecycle events
- `gate_snapshots` - Strategy gate diagnostics
- `backfill_status` - Data reconciliation monitoring
- `order_funnel_daily` - Aggregated order funnel (AggregatingMergeTree)
- `mv_order_funnel` - Materialized view for funnel

**Verification:**
```bash
clickhouse-client -q "SHOW TABLES FROM trading" | grep -E "snapshots|events|funnel"
```

### 2. Data Writers ✅
**Location:** `services/obs/writers.py`

- `ClickHouseWriter` - Batch HTTP writer with JSONEachRow format
- `LokiWriter` - Batch writer with low-cardinality labels
- Datetime handling fixed for ClickHouse compatibility
- Tested successfully with demo data

**Test:**
```bash
PYTHONPATH=$PWD venv/bin/python3 services/obs/writers.py
```

### 3. Event Contract ✅
**Location:** `libs/obs/events.py`

Defines 11 event types:
- `EventName` enum: position_snapshot, account_snapshot, gate_snapshot, order_lifecycle, etc.
- `OrderLifecycleStage` enum: created, risk_ok, submitted, filled, rejected, etc.
- `Gate` enum: A, B, C

### 4. Observability Queue ✅
**Location:** `libs/obs/queue.py`

- Non-blocking queue with batch flushing
- Two channels: LOGS (Loki), CLICKHOUSE
- Singleton pattern: `obs_queue`
- Background flushers with configurable batch size (500) and interval (2s)

### 5. Prometheus Metrics Exporter ✅
**Location:** `services/metrics/exporter.py`

Key metrics defined:
- `service_up` - Service heartbeat gauge
- `latency_signal_to_order_ms` - P95 latency tracking
- `latency_order_to_fill_ms` - Fill latency
- `clickhouse_write_latency_ms` - DB write performance
- `orders_rejected_total` - Rejection counter
- `kafka_topic_lag` - Consumer lag

Helper utilities:
- `MsTimer` context manager
- `@time_async` decorator
- `mark_service_up()`, `inc_rejected()`, etc.

### 6. Infrastructure ✅
- Prometheus installed via Homebrew
- Grafana running on port 3000
- Loki running (http://localhost:3100)
- ClickHouse running (http://localhost:8123)

## Current Focus (Q4 Consolidation)

1. **Dashboard Consolidation**
   - [x] Infrastructure & Platform Health (`grafana/dashboards/active/infrastructure_platform_health.json`)
   - [x] Trading System Operations (`grafana/dashboards/active/trading_system_operations.json`)
   - [x] Strategy & Decision Pipeline Insight (`grafana/dashboards/active/strategy_pipeline_insight.json`)
   - [x] Financial & Market Overview (`grafana/dashboards/active/financial_market_overview.json`)

2. **Panel Migration**
   - ✅ `doc/dashboard/PANEL_MATRIX.csv` captures source → target mapping.

3. **Alert Stubs**
   - ✅ Loki + Prometheus rules captured under `grafana/provisioning/alerting/`.

4. **Bridge Work (Additive Enhancements)**
   - [x] **P0** Exporters & Prom scrape jobs (`docker-compose.yml`, `infra/prometheus.yml`), promtail label promotion (`promtail/config.yml`).
   - [x] **P1** Grafana contact points & policy (`grafana/provisioning/alerting/contact-points.yaml`, `notification-policies.yaml`).
   - [x] **P2** Strategy inventory snapshot scaffold (`db/pg/strategy_status_snapshots.sql`, `scripts/update_strategy_status_snapshots.py`).
   - [x] **P3** Market index feed scaffold (`db/clickhouse/market_index_snapshots.sql`, `scripts/load_market_index_snapshots.py`).
   - [x] **P4** QA screenshots (captured via render API → `doc/dashboard/qa/*.png`).

5. **Outstanding Gaps / Next Validations**
   - Restart Prometheus/Grafana stack to confirm new exporters report metrics.
   - Ensure `strategy_status_snapshots` pipeline is scheduled (currently manual via script).
   - Keep benchmark ingestion fresh (run loader periodically or integrate with data feed).

#### 5. Instrument Strategy Engine (Gate Logging)

```python
from libs.obs.events import EventName, Gate

async def _log_gate_snapshot(self, trace_id, strategy_id, symbol, gate, passed, details):
    if passed and not self.config.get('log_passed_gates', False):
        return  # Only log failures by default

    await obs_queue.put(Channel.LOGS, {
        "event": EventName.GATE_SNAPSHOT,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "gate": gate.value,
        "passed": passed,
        "threshold": details.get('threshold'),
        "value": details.get('value'),
        "reason": details.get('reason'),
        "threshold_version": details.get('threshold_version', 'v1.0'),
        "feature_hash": details.get('feature_hash', ''),
        "decider_version": self.version,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "trace_id": trace_id
    })
```

## Testing Checklist

- [ ] ClickHouse tables created and queryable
- [ ] Writers can insert demo data successfully
- [ ] Prometheus /metrics endpoint accessible
- [ ] Grafana can query all three data sources
- [ ] Obs queue flushers run without blocking event loop
- [ ] Dashboard panels show data
- [ ] Trace IDs link Loki logs to order lifecycle

## Files Created

```
schema/clickhouse/observability/
  └── observability_schema.sql          ✅ DDL for all tables

services/obs/
  ├── __init__.py                       ✅
  └── writers.py                        ✅ ClickHouseWriter + LokiWriter

libs/obs/
  ├── __init__.py                       ✅
  ├── events.py                         ✅ Event contract enums
  └── queue.py                          ✅ Non-blocking queue

services/metrics/
  ├── __init__.py                       ✅
  └── exporter.py                       ✅ Prometheus metrics
```

## Configuration Needed

### Prometheus Config
Location: `~/prometheus.yml` or use existing Prometheus setup

```yaml
global:
  scrape_interval: 10s

scrape_configs:
  - job_name: 'trading'
    static_configs:
      - targets: ['localhost:9108']
```

### Grafana Data Sources
Add if not already configured:
1. ClickHouse: http://localhost:8123, database=trading
2. Loki: http://localhost:3100
3. Prometheus: http://localhost:9090

## Performance Expectations

- ClickHouse write latency: <50ms for 500-row batch
- Loki write latency: <20ms for 500-event batch
- Queue flush interval: 2 seconds
- Max queue size: 10,000 items per channel
- Event loop blocking: <1ms (all I/O offloaded to flushers)

## Success Criteria

✅ **Phase 1 (Infrastructure)** - Complete
- All tables created in ClickHouse
- Writers functional and tested
- Event contracts defined
- Queue framework ready

⏳ **Phase 2 (Integration)** - Next
- Prometheus running and scraping
- Services instrumented
- Grafana dashboard imported

⏳ **Phase 3 (Production)** - Future
- 5-second position snapshots publishing
- Gate failures logged and queryable
- Order funnel shows real conversions
- Latency P95 < SLO thresholds

## Resources

- Implementation Plan: `doc/dashboard/DASHBOARD_NEW_PLAN.md`
- Original Plan: `doc/dashboard/DASHBOARD_IMPLEMENTATION_PLAN.md`
- ClickHouse docs: https://clickhouse.com/docs
- Prometheus docs: https://prometheus.io/docs
- Grafana docs: https://grafana.com/docs
