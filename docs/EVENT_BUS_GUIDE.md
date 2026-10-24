# Event Bus System Guide

> **Version**: 2.0 (Enhanced with Retry, DLQ, Metrics)
> **Last Updated**: 2025-11-02

## Overview

The Event Bus is a Kafka-based publish/subscribe system for inter-component communication in the AI trading system. It supports:

- ✅ **Automatic Retry** with exponential backoff
- ✅ **Dead Letter Queue (DLQ)** for failed messages
- ✅ **Prometheus Metrics** for observability
- ✅ **Graceful degradation** when Kafka is unavailable

---

## Architecture

```
Publisher → Event Bus → Kafka Topic
                ↓ (on failure)
            Retry with backoff
                ↓ (exhausted)
            DLQ Topic (topic.dlq)
```

### Components

| Component | File | Description |
|-----------|------|-------------|
| Publisher | `libs/event_bus.py` | Core publish API with retry/DLQ/metrics |
| Configuration | `config.yml` | Event bus and Kafka settings |
| Consumer | `scripts/ai_trader/control_event_consumer.py` | Example consumer |

---

## Configuration

### Location

**File**: `config.yml`

**Sections**:
```yaml
event_bus:
  provider: kafka
  keying: symbol           # Kafka key field from message
  retries:
    max_attempts: 3        # Maximum retry attempts before DLQ
    backoff_ms:            # Exponential backoff delays
      - 1000               # 1st retry: wait 1 second
      - 5000               # 2nd retry: wait 5 seconds
      - 15000              # 3rd retry: wait 15 seconds
  topics:
    # Topic aliases (key → actual Kafka topic name)
    bars: events.bars
    fills: events.fills
    health: events.health
    plans: internal.plans
    plans_approved: internal.plans.approved
    control: internal.control
    risk_events: internal.risk.events

kafka:
  bootstrap_servers: 127.0.0.1:9092
  acks: all
  enable_idempotence: true
  compression_type: lz4
  linger_ms: 5
  batch_size: 131072
  message_timeout_ms: 15000
```

### Retry Behavior

| Attempt | Wait Time | Total Elapsed |
|---------|-----------|---------------|
| 1st | 0s (immediate) | 0s |
| 2nd | 1s | 1s |
| 3rd | 5s | 6s |
| **DLQ** | - | 21s (after 3rd attempt fails) |

**Total time before DLQ**: ~21 seconds (including send timeouts)

---

## Usage

### Basic Publish

```python
from libs.event_bus import publish

# Publish to a topic using alias
success = publish('control', {
    'action': 'BLOCK_NEW',
    'strategy_id': 'enhanced_ml_001',
    'reason': 'High volatility detected'
})

if success:
    print("Event published successfully")
else:
    print("Event failed after retries and sent to DLQ")
```

### Publish with Custom Key

```python
# Use symbol as message key for partitioning
success = publish('fills', {
    'symbol': 'TSLA',
    'side': 'BUY',
    'qty': 100,
    'price': 250.50
}, key='TSLA')
```

### Direct Topic Name

```python
# Use literal topic name (bypasses alias lookup)
success = publish('events.custom.topic', {
    'data': 'custom event'
})
```

---

## Dead Letter Queue (DLQ)

### DLQ Topic Naming

Failed messages are sent to: `<original_topic>.dlq`

**Examples**:
- `internal.control` → `internal.control.dlq`
- `events.bars` → `events.bars.dlq`

### DLQ Message Schema

```json
{
  "original_topic": "internal.control",
  "original_key": "TSLA",
  "original_value": {
    "action": "BLOCK_NEW",
    "strategy_id": "enhanced_ml_001"
  },
  "attempts": 3,
  "dlq_timestamp": 1730520000.123
}
```

### DLQ Consumer (Future)

Create a monitoring service to:
1. Consume from `*.dlq` topics
2. Log/alert on DLQ messages
3. Optionally replay messages after resolving issues

**Example Consumer Skeleton**:
```python
from kafka import KafkaConsumer
import json

consumer = KafkaConsumer(
    'internal.control.dlq',
    bootstrap_servers='127.0.0.1:9092',
    value_deserializer=lambda m: json.loads(m.decode('utf-8'))
)

for message in consumer:
    dlq_msg = message.value
    print(f"DLQ Message: {dlq_msg['original_topic']}")
    print(f"Failed after {dlq_msg['attempts']} attempts")
    print(f"Original value: {dlq_msg['original_value']}")
    # TODO: Alert, log to ClickHouse, or replay
```

---

## Prometheus Metrics

### Available Metrics

#### 1. `event_bus_publish_total` (Counter)

**Description**: Total number of publish attempts

**Labels**:
- `topic`: Kafka topic name
- `status`: `success`, `retry`, `dlq`, `failed`

**Example**:
```
event_bus_publish_total{topic="internal.control", status="success"} 1450
event_bus_publish_total{topic="internal.control", status="retry"} 23
event_bus_publish_total{topic="internal.control", status="dlq"} 2
event_bus_publish_total{topic="internal.control", status="failed"} 0
```

**PromQL Queries**:
```promql
# Success rate by topic
rate(event_bus_publish_total{status="success"}[5m])
/
rate(event_bus_publish_total[5m])

# DLQ rate (alert trigger)
rate(event_bus_publish_total{status="dlq"}[5m]) > 0.01
```

#### 2. `event_bus_retries_total` (Counter)

**Description**: Total number of retry attempts

**Labels**:
- `topic`: Kafka topic name
- `attempt`: Retry attempt number (`1`, `2`, `3`)

**Example**:
```
event_bus_retries_total{topic="internal.control", attempt="1"} 15
event_bus_retries_total{topic="internal.control", attempt="2"} 5
event_bus_retries_total{topic="internal.control", attempt="3"} 2
```

**PromQL Queries**:
```promql
# Total retries across all topics
sum(rate(event_bus_retries_total[5m])) by (topic)

# Topics with high retry rate
topk(5, sum(rate(event_bus_retries_total[5m])) by (topic))
```

#### 3. `event_bus_publish_duration_seconds` (Histogram)

**Description**: Duration of publish operations (including retries)

**Labels**:
- `topic`: Kafka topic name

**Buckets**: Default Prometheus histogram buckets (0.005s, 0.01s, 0.025s, 0.05s, 0.1s, 0.25s, 0.5s, 1s, 2.5s, 5s, 10s)

**Example**:
```
event_bus_publish_duration_seconds_sum{topic="internal.control"} 125.3
event_bus_publish_duration_seconds_count{topic="internal.control"} 1450
```

**PromQL Queries**:
```promql
# p99 latency by topic
histogram_quantile(0.99, sum(rate(event_bus_publish_duration_seconds_bucket[5m])) by (le, topic))

# Average duration
rate(event_bus_publish_duration_seconds_sum[5m])
/
rate(event_bus_publish_duration_seconds_count[5m])
```

### Metrics Endpoint

If using Prometheus exporter in the application:

```python
# In your main application (e.g., orchestrator.py)
from prometheus_client import start_http_server

# Start metrics HTTP server on port 8000
start_http_server(8000)

# Prometheus scrape config:
# scrape_configs:
#   - job_name: 'ai_trader'
#     static_configs:
#       - targets: ['localhost:8000']
```

---

## Error Handling

### Graceful Degradation

The event bus is designed for **best-effort delivery**. If Kafka is unavailable:

- ✅ `publish()` returns `False` (does not raise exception)
- ✅ Caller can decide to log, skip, or use fallback
- ✅ No blocking or crashes

**Example**:
```python
from libs.event_bus import publish
import logging

success = publish('control', {'action': 'BLOCK_NEW'})
if not success:
    logging.warning("Failed to publish control event - Kafka may be down")
    # Optional: Use fallback mechanism (e.g., database flag)
    # set_emergency_block_flag_in_db()
```

### Monitoring Failures

Use Prometheus metrics to detect:

1. **High DLQ rate**: `event_bus_publish_total{status="dlq"}`
2. **High retry rate**: `event_bus_retries_total`
3. **Complete failures**: `event_bus_publish_total{status="failed"}`

**Recommended Alert**:
```yaml
# Prometheus alerting rule
- alert: EventBusHighDLQRate
  expr: rate(event_bus_publish_total{status="dlq"}[5m]) > 0.01
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High event bus DLQ rate detected"
    description: "{{ $labels.topic }} has DLQ rate {{ $value }}/s"
```

---

## Testing

### Unit Test Example

```python
import pytest
from libs.event_bus import publish

def test_publish_success(mocker):
    # Mock Kafka producer
    mock_producer = mocker.patch('libs.event_bus.KafkaProducer')
    mock_future = mocker.Mock()
    mock_future.get.return_value = None  # Success
    mock_producer.return_value.send.return_value = mock_future

    result = publish('control', {'action': 'TEST'})

    assert result is True
    mock_producer.return_value.send.assert_called_once()

def test_publish_with_retry(mocker):
    # First attempt fails, second succeeds
    mock_producer = mocker.patch('libs.event_bus.KafkaProducer')
    mock_future = mocker.Mock()
    mock_future.get.side_effect = [Exception("Timeout"), None]
    mock_producer.return_value.send.return_value = mock_future

    result = publish('control', {'action': 'TEST'})

    assert result is True
    assert mock_producer.return_value.send.call_count == 2

def test_publish_dlq_fallback(mocker):
    # All attempts fail, DLQ succeeds
    mock_producer = mocker.patch('libs.event_bus.KafkaProducer')
    mock_future = mocker.Mock()
    # 3 retries fail, then DLQ succeeds
    mock_future.get.side_effect = [
        Exception("Timeout"),
        Exception("Timeout"),
        Exception("Timeout"),
        None  # DLQ success
    ]
    mock_producer.return_value.send.return_value = mock_future

    result = publish('control', {'action': 'TEST'})

    assert result is False  # Original send failed
    # 3 attempts + 1 DLQ = 4 sends
    assert mock_producer.return_value.send.call_count == 4
```

### Integration Test

```bash
# Start Kafka
docker-compose up -d kafka

# Create test script
cat > test_event_bus.py << 'EOF'
from libs.event_bus import publish
import time

print("Testing event bus with retry...")

# Test 1: Normal publish
success = publish('control', {'action': 'TEST', 'ts': time.time()})
print(f"Test 1 (normal): {'✅ PASS' if success else '❌ FAIL'}")

# Test 2: Stop Kafka to trigger retries
print("\nStop Kafka now and press Enter...")
input()

success = publish('control', {'action': 'TEST_RETRY', 'ts': time.time()})
print(f"Test 2 (with retries): {'DLQ sent (expected)' if not success else 'Unexpected success'}")

print("\nRestart Kafka and press Enter...")
input()

# Test 3: Kafka recovered
success = publish('control', {'action': 'TEST_RECOVERY', 'ts': time.time()})
print(f"Test 3 (recovery): {'✅ PASS' if success else '❌ FAIL'}")
EOF

python test_event_bus.py
```

---

## Troubleshooting

### Issue: All publishes return False

**Causes**:
1. Kafka broker not running
2. Network connectivity issue
3. Topic auto-create disabled and topic doesn't exist

**Solutions**:
```bash
# Check Kafka status
docker ps | grep kafka

# Check broker connectivity
telnet 127.0.0.1 9092

# List existing topics
kafka-topics.sh --bootstrap-server 127.0.0.1:9092 --list

# Create missing topics
kafka-topics.sh --bootstrap-server 127.0.0.1:9092 --create \
  --topic internal.control \
  --partitions 4 \
  --replication-factor 1
```

### Issue: High retry rate but no DLQ messages

**Causes**:
1. Intermittent network issues (resolved before max retries)
2. Kafka broker throttling
3. Message size exceeding limits

**Investigation**:
```bash
# Check Kafka broker logs
docker logs trading_kafka | grep ERROR

# Check message size distribution
# (requires Prometheus metrics)
curl localhost:8000/metrics | grep event_bus_publish_duration
```

### Issue: DLQ topics growing unbounded

**Causes**:
1. Persistent Kafka issues not being resolved
2. Consumer not processing DLQ messages

**Solutions**:
1. Set up DLQ monitoring and alerts
2. Implement DLQ consumer for replay
3. Set TTL on DLQ topics:
```bash
kafka-configs.sh --bootstrap-server 127.0.0.1:9092 \
  --entity-type topics \
  --entity-name internal.control.dlq \
  --alter --add-config retention.ms=604800000  # 7 days
```

---

## Migration Guide

### Upgrading from v1 (Basic) to v2 (Enhanced)

**Changes**:
- ✅ Automatic retry with backoff (transparent)
- ✅ DLQ topics created automatically
- ✅ Prometheus metrics (optional dependency)

**No code changes required** - existing `publish()` calls work as-is with enhanced features.

**Optional**: Add Prometheus monitoring:
```bash
# Install Prometheus client
pip install prometheus-client

# Update application to expose metrics
# (see "Metrics Endpoint" section above)
```

---

## Performance Considerations

### Throughput

- **Single publish**: ~5-10ms (with Kafka available)
- **With 1 retry**: ~1.005s (1s backoff + 5ms send)
- **With 3 retries**: ~21s (worst case before DLQ)

### Optimization Tips

1. **Batch publish**: For high-throughput scenarios, consider batching messages:
```python
# Future enhancement - not yet implemented
from libs.event_bus import publish_batch

events = [
    {'topic': 'fills', 'value': {'symbol': 'TSLA', 'qty': 100}},
    {'topic': 'fills', 'value': {'symbol': 'AAPL', 'qty': 200}},
]
publish_batch(events)  # Single producer instance, pipelined sends
```

2. **Async publish**: For non-blocking behavior:
```python
# Future enhancement - not yet implemented
from libs.event_bus import publish_async

await publish_async('control', {'action': 'BLOCK_NEW'})
```

3. **Producer pooling**: Reuse producer instances (future enhancement)

---

## Related Documents

- [CLAUDE.md](../CLAUDE.md) - Development guidelines
- [config.yml](../config.yml:106-126) - Event bus configuration
- [control_event_consumer.py](../scripts/ai_trader/control_event_consumer.py) - Example consumer

---

## Change Log

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-10-15 | Initial implementation with basic publish |
| 2.0 | 2025-11-02 | Added retry, DLQ, Prometheus metrics |
