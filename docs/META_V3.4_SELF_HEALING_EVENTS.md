# Meta v3.4: Self-Healing Event Alignment

## 📋 Overview

**Status**: ✅ Complete
**Date**: 2025-12-03
**Component**: Self-Healing Observability

Meta v3.4 ensures all self-healing actions emit standardized events to `ml.events` table for comprehensive observability and audit trail.

## 🎯 Objectives

1. **Event Schema**: Create `ml.events` table for unified event stream
2. **Event Emitter**: Provide simple API for event emission
3. **Code Integration**: Update all self-healing code paths to emit events
4. **Observability**: Enable real-time monitoring of autonomous actions

## 📁 Files Created/Modified

### New Files

| File | Lines | Description |
|------|-------|-------------|
| `schema/clickhouse/ml/ml_events.sql` | 60 | ClickHouse table schema for ml.events |
| `libs/ml/event_emitter.py` | 410 | Event emission API with convenience functions |
| `tests/integration/test_meta_v34_self_healing_events.py` | 440 | Integration tests for event emission |
| `docs/META_V3.4_SELF_HEALING_EVENTS.md` | This file | Documentation |

### Modified Files

| File | Changes | Description |
|------|---------|-------------|
| `libs/healer/data_healer_v2.py` | +30 lines | Added event emission to heal_action() |
| `libs/ml/auto_remediation.py` | +40 lines | Added event emission to remediation actions |

## 🗄️ Database Schema: ml.events

```sql
CREATE TABLE ml.events (
    ts DateTime64(3, 'UTC') DEFAULT now64(3),
    event String,                    -- Event type (e.g., 'self_heal.action_complete')
    component String DEFAULT '',     -- Component name (e.g., 'data_healer_v2')
    action_id String DEFAULT '',     -- Unique action ID for tracking

    -- Contextual metadata
    symbol String DEFAULT '',
    status String DEFAULT '',        -- 'success', 'failed', 'in_progress'
    reason String DEFAULT '',

    -- Metrics
    duration_sec Float64 DEFAULT 0,
    rows_affected Int64 DEFAULT 0,

    -- Self-healing specific
    action_type String DEFAULT '',   -- 'generate_label', 'rebuild_features', etc.
    is_auto_executed UInt8 DEFAULT 0,

    -- ML job specific
    experiment_id String DEFAULT '',
    model_name String DEFAULT '',
    sharpe Float64 DEFAULT 0,

    -- Data quality specific
    null_ratio Float64 DEFAULT 0,
    coverage_ratio Float64 DEFAULT 0,

    -- Flexible metadata (JSON)
    metadata String DEFAULT '{}'
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(ts)
ORDER BY (event, component, ts)
TTL ts + INTERVAL 90 DAY;
```

**Indexes**:
- `idx_action_id`: Bloom filter for action ID lookups
- `idx_symbol`: Bloom filter for symbol-based queries
- `idx_experiment_id`: Bloom filter for ML job tracking

## 📡 Event Emission API

### High-Level Functions

```python
from libs.ml.event_emitter import (
    emit_self_heal_event,
    emit_auto_remediation_event,
    emit_ml_job_event,
    emit_data_sync_event,
    emit_preflight_event
)

# Self-healing action
emit_self_heal_event(
    action_type="generate_label",
    action_id="heal_20251203_001",
    status="success",
    symbol="TSLA",
    duration_sec=15.3,
    rows_affected=1200,
    metadata={"horizons": [30, 60]}
)

# Auto-remediation
emit_auto_remediation_event(
    action_taken="sync_features_active",
    status="success",
    symbol="TSLA",
    duration_sec=45.2,
    reason="Features stale",
    metadata={"rows_synced": 5000}
)

# ML training job
emit_ml_job_event(
    event="ml.job.complete",
    experiment_id="exp_20251203_001",
    model_name="lightgbm",
    status="success",
    sharpe=1.85,
    duration_sec=120.5
)
```

### Convenience Functions

```python
from libs.ml.event_emitter import emit_heal_start, emit_heal_success, emit_heal_failure

# At action start
emit_heal_start(
    action_type="rebuild_features",
    action_id="heal_001",
    symbol="AAPL"
)

# On success
emit_heal_success(
    action_type="rebuild_features",
    action_id="heal_001",
    symbol="AAPL",
    duration_sec=22.5,
    rows_affected=3500
)

# On failure
emit_heal_failure(
    action_type="rebuild_features",
    action_id="heal_001",
    symbol="AAPL",
    duration_sec=5.2,
    reason="ClickHouse connection timeout"
)
```

## 🔧 Integration Points

### 1. DataHealerV2 (libs/healer/data_healer_v2.py)

**Events Emitted**:
- `self_heal.action_start`: When action begins
- `self_heal.action_complete`: On successful completion
- `self_heal.action_failed`: On failure

**Code Integration**:
```python
# At start of heal_action()
emit_heal_start(
    action_type=action.action_type,
    action_id=action.action_id,
    symbol=action.params.get("symbols", [""])[0] if action.params.get("symbols") else "",
    metadata={"priority": action.priority}
)

# On success
emit_heal_success(
    action_type=action.action_type,
    action_id=action.action_id,
    symbol=...,
    duration_sec=duration,
    rows_affected=result.rows_affected,
    metadata=result.metadata
)

# On failure
emit_heal_failure(
    action_type=action.action_type,
    action_id=action.action_id,
    symbol=...,
    duration_sec=duration,
    reason=result.error or "Unknown error",
    metadata=result.metadata
)
```

**Action Types Tracked**:
- `generate_label`: Label generation
- `rebuild_features`: Feature rebuilding
- `backfill_bars`: Bar backfilling
- `diagnose_label_quality`: Label quality diagnosis
- `change_horizon`: Horizon adjustment
- `use_threshold_labeling`: Labeling strategy change

### 2. AutoRemediator (libs/ml/auto_remediation.py)

**Events Emitted**:
- `auto_remediation.sync_features_active`: Feature sync
- `auto_remediation.window_adjustment`: Training window adjustment

**Code Integration**:
```python
# After sync completion
emit_auto_remediation_event(
    action_taken="sync_features_active",
    status="success" if remediation_result.success else "failed",
    symbol=symbol,
    duration_sec=elapsed,
    reason=remediation_result.reason,
    metadata=remediation_result.metadata
)
```

## 📊 Observability Queries

### Count of Self-Healing Actions (Last 7 Days)

```sql
SELECT
    action_type,
    status,
    count() as total,
    avg(duration_sec) as avg_duration,
    sum(rows_affected) as total_rows
FROM ml.events
WHERE event LIKE 'self_heal.%'
  AND ts >= now() - INTERVAL 7 DAY
GROUP BY action_type, status
ORDER BY total DESC;
```

### Failed Self-Healing Actions (Last 24 Hours)

```sql
SELECT
    ts,
    action_type,
    symbol,
    reason,
    duration_sec
FROM ml.events
WHERE status = 'failed'
  AND component = 'data_healer_v2'
  AND ts >= now() - INTERVAL 24 HOUR
ORDER BY ts DESC;
```

### ML Job Success Rate (Last 30 Days)

```sql
SELECT
    status,
    count() as total,
    avg(sharpe) as avg_sharpe,
    avg(duration_sec) as avg_duration
FROM ml.events
WHERE event LIKE 'ml.job.%'
  AND ts >= now() - INTERVAL 30 DAY
GROUP BY status;
```

### Auto-Remediation Trigger Frequency

```sql
SELECT
    toDate(ts) as date,
    action_type,
    count() as triggers,
    sum(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count
FROM ml.events
WHERE component = 'auto_remediator'
GROUP BY date, action_type
ORDER BY date DESC, triggers DESC;
```

## 🧪 Testing

### Integration Tests

Run the full test suite:

```bash
PYTHONPATH=. venv/bin/pytest tests/integration/test_meta_v34_self_healing_events.py -v
```

**Test Coverage**:
1. ✅ ml.events schema validation
2. ✅ Basic event emission
3. ✅ DataHealerV2 integration
4. ✅ AutoRemediator integration
5. ✅ ML job events
6. ✅ Data sync events
7. ✅ Preflight events
8. ✅ Event aggregation queries
9. ✅ Time-range queries
10. ✅ JSON metadata handling

### Manual Verification

```bash
# 1. Initialize table
clickhouse-client < schema/clickhouse/ml/ml_events.sql

# 2. Verify table exists
clickhouse-client --query "SHOW TABLES FROM ml"

# 3. Check schema
clickhouse-client --query "DESCRIBE ml.events"

# 4. Run a test heal action
python -c "
from libs.healer.data_healer_v2 import DataHealerV2
from libs.health import Action, make_action_id

healer = DataHealerV2(dry_run=True)
action = Action(
    action_id=make_action_id('test'),
    action_type='rebuild_features',
    priority=1,
    title='Test',
    description='Test event emission',
    params={'symbols': ['TSLA'], 'lookback_days': 7},
    is_safe_auto_execute=True,
    requires_approval=False
)
result = healer.heal_action(action)
print(f'Result: {result.success}')
"

# 5. Query events
clickhouse-client --query "
SELECT ts, event, action_type, status, symbol
FROM ml.events
ORDER BY ts DESC
LIMIT 10
"
```

## 🔍 Key Features

### 1. Unified Event Stream

All autonomous actions (self-healing, ML jobs, data sync) emit to single table for unified observability.

### 2. Structured Metadata

JSON metadata field allows flexible context without schema changes:

```python
metadata = {
    "horizons": [30, 60, 120],
    "lookback_days": 90,
    "performance": {
        "rows_per_sec": 1250.5,
        "total_rows": 12000
    }
}
```

### 3. Action Tracking

Every action gets unique ID for end-to-end tracing:

```sql
-- Track complete action lifecycle
SELECT event, ts, status, duration_sec
FROM ml.events
WHERE action_id = 'heal_20251203_001'
ORDER BY ts;
```

### 4. Prometheus Integration

Events can be aggregated into Prometheus metrics via exporter:

```python
# In tools/p3_p4_metrics_exporter.py
SELF_HEAL_ACTIONS_TOTAL = Counter(
    "self_heal_actions_total",
    "Total self-healing actions",
    ["action_type", "status"]
)

# Query ml.events and update metrics
for row in ch_client.query("SELECT action_type, status, count() FROM ml.events ..."):
    SELF_HEAL_ACTIONS_TOTAL.labels(
        action_type=row["action_type"],
        status=row["status"]
    ).inc(row["count"])
```

## 📈 Performance Considerations

- **Async Insertion**: Events are inserted asynchronously, won't block heal operations
- **Batching**: ClickHouse batches small inserts automatically
- **TTL**: 90-day retention to manage storage
- **Partitioning**: Monthly partitions for efficient queries
- **Indexes**: Bloom filters for fast lookups

## 🚀 Future Enhancements

1. **Alerting**: Create Prometheus alerts from ml.events aggregations
2. **Dashboards**: Grafana dashboards for self-healing trends
3. **Anomaly Detection**: Detect unusual heal patterns (e.g., sudden spike in failures)
4. **Cost Tracking**: Track compute costs per action type
5. **Success Prediction**: ML model to predict heal success based on historical patterns

## 📚 Related Documentation

- `docs/P5_META_V3_FINAL_REPORT.md`: Full P5 + Meta v3 report
- `docs/P5_META_V3_QUICKSTART.md`: Quick start guide
- `CLAUDE.md`: Code modification guidelines
- `CODE_REVIEW_REPORT.md`: Production readiness checklist

## ✅ Completion Checklist

- [x] Create ml.events table schema
- [x] Implement event_emitter.py with high-level APIs
- [x] Integrate events into DataHealerV2
- [x] Integrate events into AutoRemediator
- [x] Write 10 integration tests
- [x] Document event schema and API
- [x] Provide observability query examples
- [x] Test event emission in dry-run mode
- [x] Verify event data integrity
- [x] Document Prometheus integration path

**Total Implementation**: ~550 lines of code
**Test Coverage**: 10 integration tests
**Event Types**: 8 distinct event categories
**Components Instrumented**: 2 (DataHealerV2, AutoRemediator)

---

**Next**: Meta v3.5 - Auto-merge Whitelist Design
