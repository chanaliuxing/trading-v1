# Plans Event Publishing

> **Version**: 1.0
> **Last Updated**: 2025-11-02

## Overview

The AI Brain generates execution plans for each trading session (morning, intraday, afterhours, overnight). These plans are now automatically published to Kafka topics for downstream consumers to react to plan changes in real-time.

---

## Architecture

```
AI Brain → Generate Plan → Risk Guard → Orchestrator._persist_plan()
                                              ↓
                         ┌────────────────────┴────────────────────┐
                         ↓                                         ↓
              internal.plans (all plans)         internal.plans.approved (risk-approved)
```

### Event Flow

1. **AI Brain**: Generates a plan (providers, symbols, actions)
2. **Risk Guard**: Validates plan against risk policies
3. **Persist Plan**: Saves to file and publishes to event bus
   - **internal.plans**: All plans (including rejected ones)
   - **internal.plans.approved**: Only plans that passed risk guard

---

## Topics

### internal.plans

**Description**: All generated plans, regardless of risk approval status

**Use Cases**:
- Monitoring AI Brain decision-making patterns
- Debugging rejected plans
- Analytics on plan evolution over time

**Schema**:
```json
{
  "ts": "2025-11-02T09:15:00-05:00",
  "session": "morning",
  "providers": [
    {
      "name": "deepseek_nvidia",
      "symbols": ["TSLA", "NVDA"],
      "interval_sec": 600,
      "cycles": 30,
      "execution_prompt_id": "v3_fast_market_following"
    }
  ],
  "actions": {
    "mode": "NORMAL",
    "hedge": false
  },
  "notes": "Pre-market momentum setup"
}
```

### internal.plans.approved

**Description**: Only plans that passed risk guard validation

**Use Cases**:
- Triggering automated plan execution
- Real-time monitoring of active trading strategies
- Risk compliance auditing

**Schema**: Same as `internal.plans`, but guaranteed to have:
- Valid `providers` array (non-empty)
- Passed risk policy checks
- No blocking conditions

**Filtering Logic** (in `orchestrator.py:705`):
```python
if plan.get('providers'):
    event_bus.publish('plans_approved', payload)
```

---

## Message Schema

### Full Plan Event

```json
{
  // Timestamp (ISO 8601 with timezone)
  "ts": "2025-11-02T09:15:00-05:00",

  // Session type
  "session": "morning" | "intraday" | "afterhours" | "overnight",

  // Provider configurations
  "providers": [
    {
      "name": "deepseek_nvidia",           // Provider ID
      "symbols": ["TSLA", "NVDA"],         // Symbols to trade
      "interval_sec": 600,                 // Poll interval (seconds)
      "cycles": 30,                        // Number of cycles to run
      "execution_prompt_id": "v3_fast_market_following",  // Prompt ID
      "extras": {                          // Optional strategy params
        "max_position_size": 500
      }
    }
  ],

  // Trading actions and controls
  "actions": {
    "mode": "NORMAL" | "BLOCK_NEW" | "SIZE_DOWN" | "EXIT_ALL",
    "hedge": true | false,
    "hedge_mode": "delta_neutral" | "beta_hedge" | null,
    "size_multiplier": 1.0,              // Active when mode=SIZE_DOWN
    "exit_reason": "high_volatility"     // Present when mode=EXIT_ALL
  },

  // AI Brain's reasoning (optional)
  "notes": "Pre-market momentum setup with conservative sizing"
}
```

---

## Configuration

Plans event publishing is controlled by:

1. **Event Bus Topics** (`config.yml:115-120`):
```yaml
event_bus:
  topics:
    plans: internal.plans
    plans_approved: internal.plans.approved
```

2. **Event Bus Retries** (`config.yml:109-114`):
```yaml
event_bus:
  retries:
    max_attempts: 3
    backoff_ms: [1000, 5000, 15000]
```

---

## Consumer Examples

### Example 1: Real-Time Plan Monitor

Monitor approved plans and log provider changes:

```python
from kafka import KafkaConsumer
import json

consumer = KafkaConsumer(
    'internal.plans.approved',
    bootstrap_servers='127.0.0.1:9092',
    value_deserializer=lambda m: json.loads(m.decode('utf-8')),
    group_id='plan_monitor',
    auto_offset_reset='latest'
)

for message in consumer:
    plan = message.value
    session = plan.get('session')
    providers = plan.get('providers', [])

    print(f"[{session.upper()}] New approved plan:")
    for prov in providers:
        name = prov.get('name')
        symbols = prov.get('symbols', [])
        print(f"  - {name}: {symbols}")
```

### Example 2: Risk Compliance Auditor

Track all plans (including rejected ones) for compliance:

```python
from kafka import KafkaConsumer
from clickhouse_driver import Client
import json

consumer = KafkaConsumer(
    'internal.plans',
    bootstrap_servers='127.0.0.1:9092',
    value_deserializer=lambda m: json.loads(m.decode('utf-8')),
    group_id='compliance_auditor'
)

ch = Client(host='localhost', port=9000)

for message in consumer:
    plan = message.value

    # Store all plans in ClickHouse for audit trail
    ch.execute(
        '''
        INSERT INTO audit.plans_log (ts, session, plan_json, approved)
        VALUES (%(ts)s, %(session)s, %(plan)s, %(approved)s)
        ''',
        {
            'ts': plan['ts'],
            'session': plan['session'],
            'plan': json.dumps(plan),
            'approved': bool(plan.get('providers'))  # Has providers = approved
        }
    )
```

### Example 3: Automated Plan Validator

Compare approved plans against predefined policies:

```python
from kafka import KafkaConsumer
import json

ALLOWED_SYMBOLS = {'TSLA', 'NVDA', 'AAPL', 'MSFT', 'AMD'}
MAX_SYMBOLS_PER_PROVIDER = 3

consumer = KafkaConsumer(
    'internal.plans.approved',
    bootstrap_servers='127.0.0.1:9092',
    value_deserializer=lambda m: json.loads(m.decode('utf-8')),
    group_id='plan_validator'
)

for message in consumer:
    plan = message.value
    violations = []

    for prov in plan.get('providers', []):
        symbols = set(prov.get('symbols', []))

        # Check symbol whitelist
        if not symbols.issubset(ALLOWED_SYMBOLS):
            violations.append(f"Unauthorized symbols: {symbols - ALLOWED_SYMBOLS}")

        # Check symbol count limit
        if len(symbols) > MAX_SYMBOLS_PER_PROVIDER:
            violations.append(f"Too many symbols: {len(symbols)} > {MAX_SYMBOLS_PER_PROVIDER}")

    if violations:
        print(f"⚠️ VIOLATIONS in {plan['session']} plan:")
        for v in violations:
            print(f"  - {v}")
        # TODO: Send alert, block execution, etc.
```

---

## Monitoring & Metrics

### Prometheus Metrics (via Event Bus)

```promql
# Plan publish success rate
rate(event_bus_publish_total{topic="internal.plans", status="success"}[5m])

# Plan approval rate (approved/total)
rate(event_bus_publish_total{topic="internal.plans.approved", status="success"}[5m])
/
rate(event_bus_publish_total{topic="internal.plans", status="success"}[5m])

# Plan publish failures (including DLQ)
rate(event_bus_publish_total{topic=~"internal.plans.*", status=~"dlq|failed"}[5m])
```

### Grafana Dashboard Panels

**Panel 1: Plans Generated Per Hour**
```promql
sum(increase(event_bus_publish_total{topic="internal.plans", status="success"}[1h]))
```

**Panel 2: Plan Approval Rate**
```promql
rate(event_bus_publish_total{topic="internal.plans.approved"}[1h])
/
rate(event_bus_publish_total{topic="internal.plans"}[1h])
```

**Panel 3: Plans by Session Type**

Requires extracting session from plan messages (consumer-side metric):
```python
# In consumer, increment custom metric:
from prometheus_client import Counter

plans_by_session = Counter(
    'ai_trader_plans_total',
    'Total plans generated',
    ['session', 'approved']
)

for message in consumer:
    plan = message.value
    plans_by_session.labels(
        session=plan['session'],
        approved=str(bool(plan.get('providers')))
    ).inc()
```

---

## Debugging

### Check Plan Publishing

```bash
# Tail Kafka topic to see plans in real-time
kafka-console-consumer.sh \
  --bootstrap-server 127.0.0.1:9092 \
  --topic internal.plans \
  --from-beginning \
  --property print.timestamp=true

# Check approved plans only
kafka-console-consumer.sh \
  --bootstrap-server 127.0.0.1:9092 \
  --topic internal.plans.approved \
  --from-beginning
```

### Verify Event Bus Integration

```bash
# Check orchestrator logs for plan publishing
tail -f logs/ai_trader.log | jq 'select(.event == "plan_publish_failed")'

# Should be empty if publishing is working
```

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| No messages in `internal.plans` | Event bus not configured | Check `config.yml:event_bus.topics.plans` |
| No messages in `internal.plans.approved` | All plans rejected by risk guard | Check risk policy audit logs in `runs/` |
| High DLQ rate | Kafka broker issues | Check Kafka broker logs, restart if needed |

---

## Testing

### Unit Test

```python
import pytest
from unittest.mock import patch
from scripts.ai_trader.orchestrator import Orchestrator

def test_persist_plan_publishes_events(mocker):
    # Mock event_bus.publish
    mock_publish = mocker.patch('libs.event_bus.publish')

    orch = Orchestrator()
    plan = {
        'providers': [{'name': 'deepseek_nvidia', 'symbols': ['TSLA']}],
        'actions': {'mode': 'NORMAL'}
    }

    orch._persist_plan('morning', plan)

    # Should publish to both topics (all plans + approved)
    assert mock_publish.call_count == 2

    # First call: internal.plans
    call1 = mock_publish.call_args_list[0]
    assert call1[0][0] == 'plans'
    assert call1[0][1]['session'] == 'morning'

    # Second call: internal.plans.approved
    call2 = mock_publish.call_args_list[1]
    assert call2[0][0] == 'plans_approved'

def test_persist_plan_rejected_not_approved(mocker):
    mock_publish = mocker.patch('libs.event_bus.publish')

    orch = Orchestrator()
    # Plan without providers (rejected by risk guard)
    plan = {'actions': {'mode': 'BLOCK_NEW'}}

    orch._persist_plan('morning', plan)

    # Should only publish to internal.plans (not approved)
    assert mock_publish.call_count == 1
    assert mock_publish.call_args[0][0] == 'plans'
```

### Integration Test

```bash
# 1. Start Kafka
docker-compose up -d kafka

# 2. Create topics
kafka-topics.sh --bootstrap-server 127.0.0.1:9092 --create \
  --topic internal.plans --partitions 1 --replication-factor 1

kafka-topics.sh --bootstrap-server 127.0.0.1:9092 --create \
  --topic internal.plans.approved --partitions 1 --replication-factor 1

# 3. Run orchestrator in test mode
python scripts/ai_trader/orchestrator.py --test-plan-publish

# 4. Verify messages
kafka-console-consumer.sh \
  --bootstrap-server 127.0.0.1:9092 \
  --topic internal.plans \
  --from-beginning \
  --max-messages 1
```

---

## Migration Notes

### From File-Only to Event Publishing

**Before (v1.0)**:
- Plans only saved to `runs/YYYY-MM-DD/brain_plan_{session}.json`
- No real-time notification mechanism
- Consumers had to poll filesystem

**After (v2.0)**:
- Plans saved to file (unchanged)
- Plans also published to Kafka topics
- Real-time consumption via Kafka consumers
- Backward compatible (file format unchanged)

**No breaking changes** - existing file-based consumers continue to work.

---

## Future Enhancements

1. **Plan Diff Events**: Publish delta between consecutive plans
2. **Plan Execution Events**: Publish when providers actually start/stop
3. **Plan Performance Events**: Link plans to actual trading outcomes
4. **Plan Rollback Events**: Publish when a plan is manually overridden

---

## Related Documents

- [EVENT_BUS_GUIDE.md](./EVENT_BUS_GUIDE.md) - Event bus system documentation
- [CLAUDE.md](../CLAUDE.md) - Development guidelines
- [orchestrator.py](../scripts/ai_trader/orchestrator.py:683-709) - Implementation

---

## Change Log

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-11-02 | Initial implementation with internal.plans and internal.plans.approved topics |
