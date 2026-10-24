# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## System Overview

This is a **quantitative intraday trading platform** that executes algorithmic strategies on US equities. The system:
- Runs in Shadow (testing), Paper (simulated), or Live (real money) modes
- Uses Interactive Brokers (IBKR) for order execution
- Processes high-frequency data with 5-second bar resolution
- Combines traditional ML models (XGBoost, LSTM) with LLM-based alpha generation

---

## Architecture Layers

### 1. Strategy Layer (`strategies/`)
- **Base**: All strategies inherit from `libs/strategy/base.py:StrategyAlgorithm`
- **Plugin System**: Use `@register_strategy("name")` decorator
- **Lifecycle**: `initialize()` → `on_features()` → `on_shutdown()`
- **Context API**: Access via `self.ctx` (market data, positions, submit intents)

Key strategies:
- `strategies/premarket_momentum_v2.py` - Premarket scanners
- `strategies/tsla_v7_2.py` - Symbol-specific high-frequency
- `strategies/enhanced_model_signal_strategy.py` - ML-based alpha

### 2. Strategy Execution Engine (`services/strategy/engine.py`)
- **RealEngine**: Manages strategy lifecycle, independent cadences per strategy
- **StrategyEngine**: Polls ClickHouse for new bars, triggers `on_features()` callbacks
- Event-driven architecture using IoC pattern

### 3. Decision Pipeline (`services/decision/`)
Three-stage sequential validation:
1. **Validator** (`validator.py`): Data freshness, cost coverage checks
2. **Arbiter** (`arbiter.py`): Multi-strategy coordination, conflict resolution
3. **Translator** (`translator.py`): Converts trading intents → execution plans

Entry point: `services/decision/pipeline.py:DecisionPipelineService`

### 4. Execution Layer (`services/exec/`)
- **ExecutionService** (`execution_service.py`): Bridges decisions to order placement
- **SafetyKernel** (`safety_kernel.py`): Pre-trade risk validation
- **IB Adapter** (`adapters/ib_adapter.py`): IBKR integration
- Ghost order prevention via database tracking + idempotency

### 5. Data Infrastructure

**Market Data Pipeline** (`services/market/`):
```
Kafka Ticks → Redis → ClickHouse (1s bars) → Feature Computation (1m/5m) → Strategies
```

**Databases**:
- **ClickHouse** (`libs/db/clickhouse.py`): OLAP for bars, features, analytics (200GB+ data)
  - Tables: `bars_5s`, `bars_1m_*`, `features_1m`, `sfs.sfs_5s`, `sfs_1m_tumble`
- **PostgreSQL** (`libs/db/postgres.py`): OLTP for orders, positions, fills, audit logs
- **Redis**: State bus, cache layer, SFS 1-second bars (`sfs:1s:SYMBOL`)

**SFS (Sliding Feature Service)**:
- `services/market/sfs_1s_service.py` - Kafka → Redis 1s bars (⚠️ keep <100ms latency)
- `libs/dal/sfs_facade.py` - Strategy-facing API for sub-bar data access

---

## Development Commands

### Infrastructure Management
```bash
make up              # Start all services (ClickHouse, Kafka, Redis, PostgreSQL)
make down            # Stop all services (SAFE - preserves data)
make verify          # Check service health
make logs            # Tail Docker logs

# ⚠️ NEVER use: docker-compose down -v (deletes all data volumes!)
```

### Testing
```bash
make test                           # Run all tests
pytest tests/ -v                    # All tests with verbose output
pytest tests/unit -k strategy       # Unit tests matching "strategy"
pytest tests/e2e -m "not slow"      # E2E excluding slow tests
```

### Code Quality
```bash
black src/ tests/                   # Format code
mypy src/                           # Type checking
ruff check src/                     # Linting
```

### Data Operations
```bash
make compute-features res=1m days=3     # Compute features for 1m resolution
make compute-features-all               # All resolutions (1m, 5m, 10m, etc.)
make recon1m                            # Data reconciliation for 1m bars
make cost-day                           # Build market cost profile
```

### Runtime Log (Important)
```bash
tail -f /workspace/grafana-stack/logs/trader.log
```
- In this environment, `run_system.py` writes to `/workspace/grafana-stack/logs/trader.log`.
- Do not rely on repo-local `logs/trader.log` as the primary runtime source.

### IB Reconnect Contract (Important)
- Do not modify IB Gateway/TWS client config as a first response to reconnect failures.
- Long-lived `market` / `exec` / `master` sessions must be owned by `services/ibkr/ib_connection_manager.py`.
- Every reconnect caller must obey the gatekeeper contract:
  - gate denied => no real connect attempt
  - denial => no `report_failure()`
  - success => `report_success()`
  - failed handshake => `report_failure()`
- `run_system.py` startup readiness is not just `market + exec`; it also requires `master_connected + master_truth_complete`.
- Runbook: `docs/guides/IBKR_RECONNECT_STORM_AND_STUCK_RECOVERY_RUNBOOK_2026-04.md`

### Backups
```bash
make backup-pg-full    # PostgreSQL structured backup → backup/pg/
make backup-ch-daily   # ClickHouse daily backup → backup/clickhouse/YYYY-MM-DD/
```

### Database Access
```bash
clickhouse-client --host localhost
psql -h localhost -U trading_user -d trading_db
redis-cli
```

---

## Entry Points

### Production System
- **`run_system.py`** (1,469 lines): Main production entry
  - Starts all services (strategy engine, decision pipeline, execution, market data)
  - Graceful shutdown with signal handlers
  - Initializes observability (Prometheus, structured logging)

### ML/Backtesting
- **`wf_hpo.py`** (3,258 lines): Walk-forward HPO/backtesting engine
- **`backtest_ml_engine.py`** (1,067 lines): Ensemble model evaluation

---

## Critical Safety Rules

### 🚫 Prohibited Actions

1. **DO NOT modify public interfaces** - Keep `StrategyAlgorithm`, `BaseBroker` signatures stable
2. **DO NOT modify SFS Redis key format** (`sfs:1s:SYMBOL`) - affects all strategies
3. **DO NOT use blocking I/O in async code** - Use `asyncio.to_thread()` for sync operations
4. **DO NOT ignore exceptions** - Always log with `structlog` and re-raise
5. **DO NOT use `docker-compose down -v`** - Deletes all data volumes (use `make down`)
6. **DO NOT test in production** - Verify in Shadow mode first, then Paper Trading
7. **DO NOT modify ClickHouse/PostgreSQL schemas directly** - Use migration scripts

### ✅ Required Practices

1. **Read before editing** - NEVER propose changes to code you haven't read
2. **Test-driven development** - Write tests before fixes (TDD)
3. **Impact analysis** - Document affected components before changes
4. **Connection pooling** - Use shared pooled clients (prevents port exhaustion)
5. **Timezone awareness** - All times use `ZoneInfo("America/New_York")`
6. **None handling** - Use `Optional[T]`, explicit checks (`value.get('key', default)`)
7. **Structured logging** - Use `logger.error("event", key=value, exc_info=True)`

---

## Configuration

### File Locations
- **Main config**: `config.yml` (strategy definitions, system settings)
- **Environment**: `.env` (credentials, database connections)
- **Infrastructure**: `infra/compose.yml` (Docker services)
- **Test config**: `config_test.yml`

### Key Environment Variables
```bash
TRADING_MODE=shadow|paper|live          # Execution mode
PG_HOST=localhost                       # PostgreSQL
PG_DB=trading_db
CH_HOST=localhost                       # ClickHouse
CH_DB=trading
IB_HOST=127.0.0.1                       # Interactive Brokers
IB_PAPER_PORT=7497
IB_LIVE_PORT=7496
LOG_LEVEL=INFO
```

---

## Module Reference

### Core Framework (`libs/`)
- `libs/strategy/` - Strategy base classes, registry, context API
- `libs/db/` - Database clients (ClickHouse, PostgreSQL connection pools)
- `libs/config.py` - Configuration loading
- `libs/obs/` - Observability (logging, metrics)
- `libs/dal/` - Data access layer (market data facade, SFS facade)

### Services (`services/`)
- `services/strategy/` - Strategy execution engine
- `services/decision/` - 3-stage decision pipeline
- `services/exec/` - Order execution, safety kernel
- `services/market/` - Market data ingestion, backfill, catchup, SFS
- `services/reconciliation/` - Data quality validation
- `services/obs/` - Observability services

### Strategies (`strategies/`)
All strategy files inherit from `StrategyAlgorithm` and implement:
- `async def initialize() -> None`
- `async def on_features(symbol: str, features: Dict[str, Any]) -> None`
- `async def on_shutdown() -> None`

---

## Adding a New Strategy

1. **Create strategy file**: `strategies/my_strategy.py`
```python
from libs.strategy import StrategyAlgorithm, DecisionEnvelope, OrderStyle, make_decision_id

class MyStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        self.symbols = self.params.extras.get("symbols", ["AAPL"])
        await self.ctx.subscribe_features(self.symbols, bar_sec=60)

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        if features.get("close") > threshold:
            env = DecisionEnvelope(
                decision_id=make_decision_id(),
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                ts_bar_end_us=self.ctx.now_us(),
                target_qty=100.0,
                order_style=OrderStyle(kind="MKT"),
                freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
            )
            await self.ctx.submit_intent(env)
```

2. **Add to config.yml**:
```yaml
strategies:
  - id: "my_strategy_001"
    enabled: true
    module: "strategies.my_strategy"
    class: "MyStrategy"
    params:
      ttl_ms: 60000
      extras:
        symbols: ["AAPL"]
```

3. **Test in Shadow mode**: Check `shadow_orders` table, verify logs

---

## Strategy Context API

Available via `self.ctx` in strategy methods:

```python
# Data subscription
await ctx.subscribe_features(symbols, bar_sec=60)
bars = await ctx.history(symbol, resolution="1m", limit=200)

# Portfolio/positions
portfolio = await ctx.portfolio()
position = await ctx.position("AAPL")

# Market data
price = ctx.get_price("AAPL")

# Submit trading intent
await ctx.submit_intent(envelope)
```

---

## Common Patterns

### Async I/O Best Practices
```python
# ✅ Correct - non-blocking
async with asyncpg_pool.acquire() as conn:
    result = await conn.fetch(query)

# ✅ Correct - wrap blocking calls
result = await asyncio.to_thread(expensive_computation, data)

# ❌ Wrong - blocks event loop
time.sleep(5)
result = blocking_function()
```

### Connection Pooling
```python
# ✅ Correct - use ClickHouseDB (already uses shared pool internally)
from libs.db.clickhouse import ClickHouseDB
ch_client = ClickHouseDB()
result = ch_client.query("SELECT * FROM table")

# ✅ Correct - use pooled client directly for async operations
from libs.db.ch_pool import get_client, get_async_client
client = get_client()
async_client = await get_async_client()

# ❌ Wrong - creates new connection each time
ch_client = clickhouse_connect.get_client(host=host, port=port)
```

### Exception Handling
```python
# ✅ Correct - structured logging + re-raise
try:
    order = await broker.submit_order(order_request)
except OrderExecutionError as e:
    logger.error("order_failed", symbol=symbol, error=str(e), exc_info=True)
    raise

# ❌ Wrong - swallow exception
try:
    result = await operation()
except Exception as e:
    logger.error("error", error=str(e))
    # continues execution
```

### Timezone Handling
```python
from zoneinfo import ZoneInfo
from libs.time_utils import to_eastern, is_market_open

# ✅ Correct - timezone-aware
eastern = ZoneInfo("America/New_York")
now = datetime.now(tz=eastern)

# ❌ Wrong - naive datetime
now = datetime.now()  # timezone-unaware
```

---

## Testing Infrastructure

### Test Organization
```
tests/
├── unit/         # Fast, mocked tests (<1s each)
├── integration/  # Tests with Docker services
├── e2e/          # End-to-end workflow tests
├── fixtures/     # Shared test data & mocks
└── scenarios/    # Domain-specific test scenarios
```

### Pytest Markers
```python
@pytest.mark.unit           # Unit test
@pytest.mark.integration    # Integration test
@pytest.mark.e2e            # End-to-end test
@pytest.mark.slow           # Slow-running test
```

### Coverage Requirements
- Core modules (strategy, decision, exec): ≥80%
- Data pipeline: ≥70%
- Strategies: ≥60%
- Utilities: ≥90%

---

## Performance Considerations

### Critical Latency Targets
- **Decision Pipeline**: P99 < 500ms
- **SFS-1s Flush**: P99 < 100ms
- **Order Submission**: < 200ms

### Memory Management
- Explicit model cleanup in ML code: `del model; gc.collect()`
- Limit concurrent operations: `asyncio.Semaphore(10)`
- Monitor Docker container memory limits

### Database Query Optimization
- Use parameterized queries (prevent SQL injection)
- Set query timeouts: `settings={'max_execution_time': 30}`
- Batch operations when possible
- Use connection pooling (already configured)

---

## Observability

### Structured Logging
```python
from libs.obs.logging import logger

logger.info("strategy_signal",
    strategy_id=strategy_id,
    symbol=symbol,
    signal_strength=0.75,
    target_qty=100
)
```

### Prometheus Metrics
Key metrics collected:
- `sfs1s_ticks_total{symbol}` - SFS tick processing
- `sfs1s_flush_latency_ms` - Redis write latency
- `pipeline_latency_seconds` - Decision pipeline latency
- `redis_memory_used_bytes` - Redis memory usage

### Log Locations
- **System logs**: `logs/trading_system.log` (JSON format)
- **Grafana Loki**: Aggregated logs from all services

---

## Troubleshooting

### Check System Health
```bash
./scripts/check_env.sh              # Verify environment
python temp/check_ibkr_status.py    # Check IBKR connection
```

### View Recent Logs
```bash
tail -f logs/trading_system.log | jq .
```

### Query Databases
```bash
# Check recent bars
clickhouse-client --query "SELECT * FROM trading.bars_1m WHERE symbol='AAPL' ORDER BY ts DESC LIMIT 5"

# Check positions
psql -h localhost -U trading_user -d trading_db -c "SELECT * FROM positions WHERE symbol='AAPL';"

# Check Redis SFS data
redis-cli LINDEX "sfs:1s:AAPL" 0
```

### Debug Strategy Execution
1. Check strategy enabled in `config.yml`
2. Verify data subscription: `await ctx.subscribe_features(symbols, bar_sec=60)`
3. Check ClickHouse for new bars in subscribed resolution
4. Review logs for strategy_id
5. Check `shadow_orders` table in PostgreSQL for generated intents

---

## Related Documentation

- `README_ARCHITECTURE.md` - Detailed architecture guide
- `doc/regression_ml_gates.md` - ML model gating system
- `docs/POSTGRES_BACKUP_SETUP.md` - PostgreSQL backup procedures
- `docs/CLICKHOUSE_BACKUP_SETUP.md` - ClickHouse backup procedures
- `tests/README.md` - Testing guidelines

---

## Important Notes

### Docker Infrastructure
- **Config location**: `infra/compose.yml` (⚠️ NOT root `docker-compose.yml`)
- **Data volumes**: ClickHouse data at `/workspace/Documents/clickhouse_data` (200GB+)
- **All docker-compose commands MUST be run from `infra/` directory or use `make` commands**

### Database Schema Changes
- PostgreSQL: Use Alembic migrations (planned)
- ClickHouse: Add fields with `ALTER TABLE`, never drop columns
- New columns must have defaults or allow NULL
- Always provide rollback scripts

### Async/Await Patterns
- All I/O operations use `asyncio`
- Blocking operations wrapped in `asyncio.to_thread()`
- Connection pools prevent resource leaks
- Event loop monitoring for blocked callbacks

### Security
- API keys from environment variables (`.env`)
- Parameterized database queries only
- All external calls have timeouts
- Input validation using Pydantic models

---

**Last Updated**: 2025-12-08
**System Version**: Python 3.13 | ClickHouse 24.x | PostgreSQL 14+
