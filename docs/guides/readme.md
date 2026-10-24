# Trading System - Logging Configuration

## Overview

The logging system is configured to provide **clean, operational logs** with **detailed strategy telemetry** while suppressing noisy data pipeline operations.

## Log Levels by Component

### 🔇 QUIET (WARNING+ only)
These components only log warnings and errors to keep logs clean:

- **Data Pipeline**:
  - `services.market.backfill_service` - Backfill operations
  - `services.market.catchup_service` - Gap detection/catchup
  - `services.market.writer.ch_writer` - ClickHouse bar writers
  - `services.market.writer.lake_writer` - Data lake writers
  - `services.market.features.feature_calculator` - Feature calculations
  - `services.market.features.feature_publisher` - Feature publishing
  - `services.market.features.main` - Feature service main
  - `services.market.aggregator.five_sec_agg` - 5-second aggregator

- **Infrastructure**:
  - `libs.db.clickhouse` - ClickHouse client operations
  - `libs.marketdata.vix_ingestor` - VIX data ingestion
  - `aiokafka.*` - Kafka client library (all submodules)
  - `kafka` - Kafka client library
  - `urllib3` - HTTP client library
  - `ib_insync.*` - IB Gateway client library (all submodules)
  - `py.warnings` - Python warnings

### 📋 READABLE (INFO level)
Service lifecycle and important operations:

- `__main__` - System startup/shutdown
- `services.market.market_data_service` - Market data service lifecycle
- `services.market.ib_market_data` - IB connection status

### 📊 DETAILED (INFO+ level)
Strategy execution with full telemetry:

- `strategies.*` - All strategy modules
- `services.strategy.*` - Strategy engine
- `libs.strategy.*` - Strategy infrastructure

## Controlling Log Levels

### Environment Variables

```bash
# Set overall log level (default: INFO)
export LOG_LEVEL=INFO

# Enable concise mode (console shows only WARN/ERROR, file gets everything)
export LOG_CONCISE=1

# Set custom log file path
export AUDIT_LOG_PATH=~/grafana-stack/logs/trader.log
```

### Temporary Debug Mode

To temporarily enable verbose logging for a specific component:

```python
import logging

# Enable debug logging for backfill service during troubleshooting
logging.getLogger("services.market.backfill_service").setLevel(logging.INFO)
```

## Log Output

### Console Output
- **Human-readable** format with colors
- Shows all INFO+ messages (or WARNING+ with `LOG_CONCISE=1`)
- Great for development and monitoring

### File Output
- **JSON format** at `~/grafana-stack/logs/trader.log`
- Ingested by Promtail → Loki → Grafana
- Includes all structured fields for querying

## Strategy Logging Best Practices

### Signal Evaluation (when strategy runs but doesn't fire)

```python
logger.info(
    "signal_evaluated",
    symbol=symbol,
    reason="rvol<2 or below VWAP",
    rvol=1.5,
    price=150.25,
    vwap=151.00
)
```

### Signal Fired (when strategy generates a signal)

```python
logger.info(
    "signal_fired",
    symbol=symbol,
    side="BUY",
    reason="breakout>premkt_high & above VWAP & rvol>=2",
    entry_price=152.50,
    size=100
)
```

### Order Lifecycle

```python
# Order queued
logger.info("order_queued", order_id=order_id, symbol=symbol, side="BUY", qty=100)

# Order submitted to broker
logger.info("order_submitted", order_id=order_id, symbol=symbol, broker_order_id="IB12345")

# Order filled
logger.info("order_filled", order_id=order_id, symbol=symbol, fill_price=152.50, fill_qty=100)

# Order rejected
logger.warning("order_rejected", order_id=order_id, symbol=symbol, reason="insufficient buying power")
```

### Order Why-Not-Fired

```python
logger.info(
    "order_not_placed",
    symbol=symbol,
    reason="risk_limit_exceeded",
    current_exposure=50000,
    max_exposure=45000
)
```

## Useful Loki Queries

### Service Status
```logql
{job="trader", logger="__main__"} |= "started" or |= "stopped"
```

### Strategy Decisions for TSLA
```logql
{job="trader", logger=~"strategies.*"} | json | symbol="TSLA"
```

### All Fired Signals
```logql
{job="trader"} | json | event="signal_fired"
```

### Order Timeline
```logql
{job="trader"} | json | event=~"order_queued|order_submitted|order_filled|order_rejected"
```

### Errors Only
```logql
{job="trader"} | json | level="error"
```

### Backfill Problems
```logql
{job="trader", logger=~"services.market.backfill.*"} | json | level=~"warning|error"
```

## Modifying Log Levels

Edit `boot_logging.py` to adjust which components are quiet/verbose:

```python
# To make backfill verbose again:
_QUIET_LOGGERS = [
    # "services.market.backfill_service",  # <-- comment this out
    "services.market.catchup_service",
    # ... rest
]

# To quiet down strategies:
_STRATEGY_LOGGERS = []  # <-- clear this list
logging.getLogger("strategies").setLevel(logging.WARNING)
```

## Testing Log Configuration

Quick test to verify IB Gateway connection:

```bash
source venv/bin/activate
python testib.py
```

Run system for 10 seconds:

```bash
source venv/bin/activate
timeout 10 python run_system.py || true
```


## Troubleshooting

### Too Quiet?
Set `LOG_LEVEL=DEBUG` temporarily:
```bash
LOG_LEVEL=DEBUG python run_system.py
```

### Still Too Noisy?
Add more loggers to `_QUIET_LOGGERS` in `boot_logging.py`:
```python
_QUIET_LOGGERS = [
    # ... existing ...
    "noisy.module.name",  # <-- add here
]
```

### Strategy Logs Not Showing?
Check that your strategy logger uses the right namespace:
```python
from libs.logger import get_logger
logger = get_logger("strategies.my_strategy")  # <-- must start with "strategies"
```