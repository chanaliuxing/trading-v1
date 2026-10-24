from __future__ import annotations
"""
Event bus publisher with retry, DLQ, and metrics.

Features:
- Configurable retry with exponential backoff (config.yml: event_bus.retries)
- Dead Letter Queue (DLQ) for failed messages after max retries
- Prometheus metrics for observability
- Falls back to no-op if Kafka client not available
"""
import time
import json
from typing import Any, Dict, Optional
import os
from libs.config import config
from libs.logger import get_logger

logger = get_logger("event_bus")

# Prometheus metrics (optional dependency)
try:
    from prometheus_client import Counter, Histogram
    METRICS_AVAILABLE = True

    # Define metrics
    event_bus_publish_total = Counter(
        'event_bus_publish_total',
        'Total number of event bus publish attempts',
        ['topic', 'status']  # status: success, retry, dlq, failed
    )

    event_bus_publish_duration_seconds = Histogram(
        'event_bus_publish_duration_seconds',
        'Duration of event bus publish operations',
        ['topic']
    )

    event_bus_retries_total = Counter(
        'event_bus_retries_total',
        'Total number of retry attempts',
        ['topic', 'attempt']
    )
except ImportError:
    METRICS_AVAILABLE = False


def _topic_from_key(topic_key: str) -> str:
    topics = config.get('event_bus.topics', {}) or {}
    # if key matches a known alias (e.g., 'control'), return mapped topic
    if isinstance(topics, dict) and topic_key in topics:
        return topics.get(topic_key)  # type: ignore
    # else assume it's a literal topic name
    return topic_key


def _get_dlq_topic(topic: str) -> str:
    """Get DLQ topic name for a given topic."""
    return f"{topic}.dlq"


def _send_to_kafka(producer, topic: str, key: Optional[str], value: Dict[str, Any]) -> bool:
    """Low-level send to Kafka with error handling."""
    try:
        future = producer.send(topic, key=key, value=value)
        # Wait for send to complete with timeout
        metadata = future.get(timeout=5)
        logger.debug("kafka_send_success", topic=topic, partition=metadata.partition, offset=metadata.offset)
        return True
    except Exception as e:
        logger.error("kafka_send_failed", topic=topic, error=str(e), error_type=type(e).__name__, exc_info=True)
        return False


def _publish_with_retry(topic: str, value: Dict[str, Any], key: Optional[str] = None) -> bool:
    """Publish with retry logic and DLQ fallback."""
    from kafka import KafkaProducer  # type: ignore

    kc = config.kafka_config
    retry_cfg = config.get('event_bus.retries', {}) or {}
    max_attempts = retry_cfg.get('max_attempts', 3)
    backoff_ms = retry_cfg.get('backoff_ms', [1000, 5000, 15000])

    # Ensure backoff list is long enough
    while len(backoff_ms) < max_attempts:
        backoff_ms.append(backoff_ms[-1] if backoff_ms else 15000)

    producer = KafkaProducer(
        bootstrap_servers=kc.get('bootstrap_servers') or ['127.0.0.1:9092'],
        value_serializer=lambda v: json.dumps(v).encode('utf-8'),
        key_serializer=lambda k: k.encode('utf-8') if isinstance(k, str) else k,
        linger_ms=kc.get('linger_ms', 5),
        compression_type=str(kc.get('compression_type', 'lz4')),
    )

    try:
        # Attempt send with retries
        for attempt in range(max_attempts):
            success = _send_to_kafka(producer, topic, key, value)

            if success:
                if METRICS_AVAILABLE:
                    event_bus_publish_total.labels(topic=topic, status='success').inc()
                    if attempt > 0:
                        event_bus_retries_total.labels(topic=topic, attempt=str(attempt)).inc()
                return True

            # Failed - retry with backoff if not last attempt
            if attempt < max_attempts - 1:
                if METRICS_AVAILABLE:
                    event_bus_publish_total.labels(topic=topic, status='retry').inc()
                    event_bus_retries_total.labels(topic=topic, attempt=str(attempt + 1)).inc()
                time.sleep(backoff_ms[attempt] / 1000.0)  # Convert ms to seconds

        # All retries exhausted - send to DLQ
        dlq_topic = _get_dlq_topic(topic)
        dlq_value = {
            'original_topic': topic,
            'original_key': key,
            'original_value': value,
            'attempts': max_attempts,
            'dlq_timestamp': time.time(),
        }

        dlq_success = _send_to_kafka(producer, dlq_topic, key, dlq_value)

        if METRICS_AVAILABLE:
            if dlq_success:
                event_bus_publish_total.labels(topic=topic, status='dlq').inc()
            else:
                event_bus_publish_total.labels(topic=topic, status='failed').inc()

        return False

    finally:
        producer.flush(5)
        producer.close()


def publish(topic_key: str, value: Dict[str, Any], key: Optional[str] = None) -> bool:
    """Publish a JSON-serializable payload to Kafka topic derived from topic_key.

    Features:
    - Automatic retry with exponential backoff (configured in config.yml)
    - Dead Letter Queue (DLQ) for messages that fail after max retries
    - Prometheus metrics for observability (optional)

    Returns True on successful publish, False otherwise (including DLQ sends).
    """
    # Quick escape hatch to disable Kafka in local/test runs
    if os.getenv('DISABLE_EVENT_BUS', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        return False
    topic = _topic_from_key(topic_key)
    start_time = time.time()

    try:
        result = _publish_with_retry(topic, value, key)

        if METRICS_AVAILABLE:
            duration = time.time() - start_time
            event_bus_publish_duration_seconds.labels(topic=topic).observe(duration)

        return result

    except Exception:
        # Best-effort: no-op if Kafka not available
        if METRICS_AVAILABLE:
            event_bus_publish_total.labels(topic=topic, status='failed').inc()
        return False
