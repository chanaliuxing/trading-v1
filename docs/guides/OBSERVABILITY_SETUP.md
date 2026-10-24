# Grafana Observability Stack Configuration

Complete setup documentation for the Grafana + Loki + Promtail + Tempo observability stack for the trading system.

## Overview

**Stack Location:** `~/grafana-stack/`
**Trading System:** `~/Documents/trading/run_system.py`
**Log Output:** `~/grafana-stack/logs/trader.log`

## Architecture

```
Trading System (run_system.py)
    │
    ├─> Logs (JSON) ──> Promtail ──> Loki ──> Grafana
    │
    └─> Traces (OTLP) ──> Tempo ──> Grafana
```

## Services

| Service | Port | Purpose | Health Check |
|---------|------|---------|--------------|
| Grafana | 3000 | Visualization UI | http://localhost:3000/api/health |
| Loki | 3100 | Log aggregation | http://localhost:3100/ready |
| Tempo | 3200 | Distributed tracing | http://localhost:3200/ready |
| Promtail | 9080 | Log shipper | http://localhost:9080/targets |

## Quick Start

### Start All Services
```bash
cd ~/grafana-stack
./start-stack.sh
```

### Stop All Services
```bash
cd ~/grafana-stack
./stop-stack.sh
```

### Check Status
```bash
cd ~/grafana-stack
./status-stack.sh
```

---

## Configuration Files

### 1. Loki Configuration

**File:** `~/grafana-stack/loki/config.yml`

```yaml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  path_prefix: /workspace/grafana-stack/loki
  storage:
    filesystem:
      chunks_directory: /workspace/grafana-stack/loki/chunks
      rules_directory: /workspace/grafana-stack/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

storage_config:
  tsdb_shipper:
    active_index_directory: /workspace/grafana-stack/loki/tsdb-active
    cache_location: /workspace/grafana-stack/loki/tsdb-cache
  filesystem:
    directory: /workspace/grafana-stack/loki/chunks

limits_config:
  reject_old_samples: true
  reject_old_samples_max_age: 168h     # 7 days
  retention_period: 168h                # 7 days
  allow_structured_metadata: false

compactor:
  working_directory: /workspace/grafana-stack/loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
```

**Key Settings:**
- **Retention:** 7 days (168h)
- **Storage:** Local filesystem at `~/grafana-stack/loki/`
- **Schema:** TSDB v13 (latest)
- **Port:** 3100

---

### 2. Tempo Configuration

**File:** `~/grafana-stack/tempo/config.yml`

```yaml
# ==============================
# Tempo Local Config (macOS)
# Compatible with v2.9.0-rc.0
# ==============================

server:
  http_listen_port: 3200
  grpc_listen_port: 9097

distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:4318
        grpc:
          endpoint: 0.0.0.0:4317

storage:
  trace:
    backend: local
    wal:
      path: /workspace/grafana-stack/tempo/wal
    local:
      path: /workspace/grafana-stack/tempo/blocks

compactor:
  compaction:
    block_retention: 168h    # 7 days

metrics_generator:
  registry:
    external_labels:
      source: tempo
  storage:
    path: /workspace/grafana-stack/tempo/metrics

overrides:
  max_traces_per_user: 100000
  max_bytes_per_trace: 50000000
```

**Key Settings:**
- **OTLP Receivers:** HTTP (4318), gRPC (4317)
- **Retention:** 7 days (168h)
- **Storage:** Local filesystem at `~/grafana-stack/tempo/`
- **Ports:** HTTP 3200, gRPC 9097

---

### 3. Promtail Configuration

**File:** `~/grafana-stack/promtail/config.yml`

```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /workspace/grafana-stack/promtail/positions.yaml

clients:
  - url: http://localhost:3100/loki/api/v1/push

scrape_configs:
  - job_name: trader
    static_configs:
      - targets:
          - localhost
        labels:
          job: trader
          __path__: /workspace/grafana-stack/logs/*.log

  - job_name: system
    static_configs:
      - targets:
          - localhost
        labels:
          job: macos_system
          host: trading-workstation
          __path__: /var/log/*.log

  - job_name: tempo
    static_configs:
      - targets:
          - localhost
        labels:
          job: tempo
          __path__: /workspace/grafana-stack/tempo/*.log

  - job_name: loki
    static_configs:
      - targets:
          - localhost
        labels:
          job: loki
          __path__: /workspace/grafana-stack/loki/*.log
```

**Key Settings:**
- **Port:** 9080
- **Loki Endpoint:** http://localhost:3100
- **Jobs:**
  - `trader`: Trading system logs from `~/grafana-stack/logs/*.log`
  - `system`: macOS system logs
  - `tempo`: Tempo service logs
  - `loki`: Loki service logs

---

## Integration with Trading System

### 1. Python Logging Setup

Create `obs/logging_setup.py` in your trading project:

```python
import logging, json, socket, time, os

_RUN_ID = os.getenv("RUN_ID", str(int(time.time())))
_HOST = socket.gethostname()

class JSONFormatter(logging.Formatter):
    def format(self, record):
        base = {
            "ts": int(time.time() * 1000),
            "level": record.levelname,
            "host": _HOST,
            "run_id": getattr(record, "run_id", _RUN_ID),
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
        }
        # Attach trace_id/span_id from OTEL context if present
        try:
            from opentelemetry import trace as _trace
            span = _trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.is_valid:
                base["trace_id"] = format(ctx.trace_id, "032x")
                base["span_id"]  = format(ctx.span_id, "016x")
        except Exception:
            pass
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            base.update(extra)
        return json.dumps(base, ensure_ascii=False, separators=(",", ":"))

_loggers = {}
def get_logger(name: str, level="INFO"):
    if name in _loggers:
        return _loggers[name]
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler
    sh = logging.StreamHandler()
    sh.setFormatter(JSONFormatter())

    # File handler for Promtail
    fh = logging.FileHandler(os.path.expanduser("~/grafana-stack/logs/trader.log"))
    fh.setFormatter(JSONFormatter())

    logger.handlers[:] = [sh, fh]
    logger.propagate = False
    _loggers[name] = logger
    return logger
```

### 2. OpenTelemetry Tracing Setup

Install dependencies:
```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp opentelemetry-instrumentation-asyncio
```

Create `obs/tracing.py`:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
import os

def setup_tracing(service_name="trader", endpoint=None):
    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{endpoint}/v1/traces",
                timeout=5
            )
        )
    )
    trace.set_tracer_provider(provider)

    try:
        from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
        AsyncioInstrumentor().instrument()
    except Exception:
        pass

    return trace.get_tracer(service_name)
```

### 3. Integrate into run_system.py

Add to `run_system.py`:

```python
# At the top
from obs.logging_setup import get_logger
from obs.tracing import setup_tracing
from opentelemetry import trace

# Initialize
log = get_logger("trader")
tracer = setup_tracing(service_name="trader", endpoint="http://localhost:4318")

# Example usage
def on_bar(bar):
    with tracer.start_as_current_span("process_bar", attributes={"symbol": bar.symbol}):
        log.info("bar_received", extra={"event": "bar", "symbol": bar.symbol, "close": bar.close})

        # Your processing logic
        with tracer.start_as_current_span("inference"):
            prediction = model.predict(bar)
            log.info("inference_complete", extra={"symbol": bar.symbol, "prediction": prediction})

        with tracer.start_as_current_span("strategy"):
            decision = strategy.decide(prediction)
            if decision.order:
                log.info("order_placed", extra={"event": "order", "symbol": bar.symbol, "side": decision.side})
            else:
                log.info("order_skipped", extra={"event": "order_skipped", "reason": decision.reason})
```

---

## Grafana Setup

### 1. Add Data Sources

Open http://localhost:3000 → **Connections** → **Add data source**

#### Loki
- **URL:** `http://localhost:3100`
- **Save & Test**

#### Tempo
- **URL:** `http://localhost:3200`
- **Save & Test**

#### Optional: ClickHouse
- **Server address:** `localhost` (no http://)
- **Server port:** `8123`
- **Protocol:** `HTTP`
- **Database:** `default` or `trading`
- **Username:** `default`
- **Password:** (leave blank)

### 2. Configure Trace Correlation

In **Grafana → Data sources → Loki → Derived fields**:

- **Name:** `trace_id`
- **Regex:** `"trace_id":"([a-f0-9]{32})"`
- **Internal link:** Enable
- **Datasource:** Tempo
- **URL (optional):** `/explore?left=["now-1h","now","Tempo",{"query":"${__value.raw}"}]`

This makes `trace_id` fields in logs clickable, linking directly to traces.

---

## Testing

### 1. Test Log Pipeline

Write a test log:
```bash
echo '{"ts":1234567890,"level":"INFO","event":"test","msg":"hello grafana"}' >> ~/grafana-stack/logs/trader.log
```

Query in Grafana → Explore → Loki:
```logql
{job="trader"}
```

### 2. Test Trace Pipeline

From your Python code:
```python
from obs.logging_setup import get_logger
from obs.tracing import setup_tracing

log = get_logger("test")
tracer = setup_tracing("test")

with tracer.start_as_current_span("test_operation"):
    log.info("Testing observability", extra={"event": "test"})
```

Query in Grafana → Explore → Tempo:
- Search for recent traces
- Filter by service: `test`

### 3. Verify Promtail Targets

Visit http://localhost:9080/targets

You should see:
- ✅ **trader** job (1/1 ready) pointing to `~/grafana-stack/logs/*.log`
- ✅ **system** job
- ✅ **tempo** job
- ✅ **loki** job

---

## Useful Queries

### LogQL (Loki)

```logql
# All trader logs
{job="trader"}

# Errors only
{job="trader"} | json | level="ERROR"

# Orders placed
{job="trader"} | json | event="order"

# Orders skipped with reason
{job="trader"} | json | event="order_skipped" | line_format "{{.symbol}}: {{.reason}}"

# Rate of orders per minute
rate({job="trader"} | json | event="order"[1m])

# Count by event type
sum by (event) (count_over_time({job="trader"} | json [5m]))
```

### TraceQL (Tempo)

```traceql
# All traces from trader service
{service.name="trader"}

# Slow operations (>1s)
{service.name="trader" && duration > 1s}

# Traces with errors
{service.name="trader" && status=error}

# Specific span name
{service.name="trader" && name="process_bar"}
```

---

## Troubleshooting

### Services not starting

Check logs:
```bash
tail -f ~/grafana-stack/loki/loki.log
tail -f ~/grafana-stack/tempo/tempo.log
tail -f ~/grafana-stack/promtail/promtail.log
```

### No logs appearing in Grafana

1. Check Promtail targets: http://localhost:9080/targets
2. Verify log file exists: `ls -la ~/grafana-stack/logs/trader.log`
3. Check Loki health: `curl http://localhost:3100/ready`
4. Promtail only tails NEW lines - append a test line or delete `positions.yaml` and restart

### Traces not appearing

1. Check Tempo health: `curl http://localhost:3200/ready`
2. Verify OTLP endpoint: `curl http://localhost:4318/v1/traces`
3. Check your code is calling `setup_tracing()` at startup
4. Ensure spans are being created with `tracer.start_as_current_span()`

### High disk usage

Adjust retention in configs:
- **Loki:** `limits_config.retention_period: 168h` → reduce to `24h` or `72h`
- **Tempo:** `compactor.compaction.block_retention: 168h` → reduce similarly

---

## Directory Structure

```
~/grafana-stack/
├── loki/
│   ├── config.yml          # Loki configuration
│   ├── loki.log            # Loki service logs
│   ├── chunks/             # Log data
│   ├── tsdb-active/        # Active index
│   ├── tsdb-cache/         # Index cache
│   └── compactor/          # Compaction work dir
│
├── tempo/
│   ├── config.yml          # Tempo configuration
│   ├── tempo.log           # Tempo service logs
│   ├── wal/                # Write-ahead log
│   ├── blocks/             # Trace blocks
│   └── metrics/            # Metrics generator data
│
├── promtail/
│   ├── config.yml          # Promtail configuration
│   ├── promtail.log        # Promtail service logs
│   └── positions.yaml      # File read positions
│
├── logs/
│   └── trader.log          # Your app writes here
│
├── dashboards/             # Import dashboard JSONs here
│
├── start-stack.sh          # Start all services
├── stop-stack.sh           # Stop all services
└── status-stack.sh         # Check service status
```

---

## Advanced Configuration

### Custom Log Labels

Add labels in `promtail/config.yml`:

```yaml
scrape_configs:
  - job_name: trader
    static_configs:
      - targets:
          - localhost
        labels:
          job: trader
          environment: production
          strategy: ml_v2
          __path__: /workspace/grafana-stack/logs/*.log
```

### Pipeline Stages (Log Parsing)

```yaml
scrape_configs:
  - job_name: trader
    pipeline_stages:
      - json:
          expressions:
            level: level
            event: event
            symbol: symbol
      - labels:
          level:
          event:
          symbol:
    static_configs:
      - targets:
          - localhost
        labels:
          job: trader
          __path__: /workspace/grafana-stack/logs/*.log
```

This extracts JSON fields as searchable labels.

### Span Attributes

Add rich context to traces:

```python
with tracer.start_as_current_span(
    "strategy_decision",
    attributes={
        "symbol": "AAPL",
        "model_version": "v2.1",
        "confidence": 0.85,
        "decision": "buy",
        "quantity": 100
    }
):
    # Your logic
```

---

## Production Checklist

- [ ] All services start automatically on boot (use launchd or systemd)
- [ ] Log rotation configured (use logrotate or macOS newsyslog)
- [ ] Retention periods set appropriately for disk space
- [ ] Dashboards imported and tested
- [ ] Alerts configured for critical errors
- [ ] Backup strategy for configuration files
- [ ] Monitoring for the monitoring stack itself
- [ ] Documentation updated with team-specific queries

---

## Resources

- [Loki Documentation](https://grafana.com/docs/loki/latest/)
- [Tempo Documentation](https://grafana.com/docs/tempo/latest/)
- [Promtail Documentation](https://grafana.com/docs/loki/latest/clients/promtail/)
- [OpenTelemetry Python](https://opentelemetry.io/docs/instrumentation/python/)
- [LogQL Reference](https://grafana.com/docs/loki/latest/logql/)
- [TraceQL Reference](https://grafana.com/docs/tempo/latest/traceql/)

---

**Last Updated:** 2025-10-11
**Stack Version:** Grafana 12.2.0, Loki 3.x, Tempo 2.9.0, Promtail 3.x
