# Trading System Codebase Structure Analysis

## Executive Summary
This is a sophisticated quantitative trading platform supporting multiple asset classes and trading modes (shadow/paper/live). The system is built on Python 3.13 with async/await patterns, ClickHouse for OLAP analytics, PostgreSQL for transactional data, and Interactive Brokers for execution. The architecture uses an event-driven strategy engine with independent execution cadences per strategy.

---

## 1. PROJECT ARCHITECTURE

### 1.1 Core Directory Structure

```
/trading
├── libs/                      # Shared utilities and abstractions
│   ├── db/                   # Database clients (ClickHouse, PostgreSQL, Redis)
│   ├── dal/                  # Data access layer (market_data_facade.py)
│   ├── strategy/             # Strategy framework and base classes
│   ├── cache/                # Price cache for low-latency L1 data
│   ├── marketdata/           # Market data ingestion (VIX, bars, quotes)
│   ├── obs/                  # Observability (queue, events)
│   ├── logger.py             # Structured logging (structlog + JSON)
│   ├── config.py             # Configuration singleton
│   └── checkpoint.py         # State persistence
│
├── services/                 # Domain-specific microservices
│   ├── strategy/             # Strategy execution engine
│   │   ├── engine.py        # Main RealEngine and StrategyEngine
│   │   ├── strategy_base.py # StrategyAlgorithm base class
│   │   └── rule_engine.py   # Rule-based execution logic
│   ├── exec/                # Order execution and reconciliation
│   ├── decision/            # Decision pipeline (validator, arbiter, risk)
│   ├── market/              # Market data streaming
│   ├── risk/                # Risk management and position limits
│   ├── portfolio/           # Portfolio analytics
│   ├── metrics/             # Performance metrics
│   ├── compliance/          # Regulatory compliance checks
│   ├── reconciliation/      # Trade and position reconciliation
│   ├── backtest/            # Backtesting engine
│   ├── ibkr/                # Interactive Brokers adapter
│   └── analytics/           # Analytics and reporting
│
├── strategies/              # Trading strategy implementations
│   ├── premarket_momentum_v2.py  # Premarket scanning strategy
│   ├── tsla_v7_2.py             # TSLA-specific strategy
│   ├── aapl_dividend_wave.py    # Dividend event strategy
│   ├── model_signal_strategy.py # ML model-based signals
│   └── configs/                 # Strategy configuration files
│
├── infra/                   # Infrastructure & DevOps
│   ├── docker-compose.yml   # Main service orchestration
│   ├── clickhouse-config/   # ClickHouse configuration
│   ├── prometheus/          # Prometheus monitoring setup
│   ├── registry/            # Schema registry
│   └── vault/               # HashiCorp Vault setup
│
├── tests/                   # Test suite
│   ├── e2e_exec/           # End-to-end execution tests
│   ├── e2e_market/         # End-to-end market data tests
│   ├── e2e_strategy/       # End-to-end strategy tests
│   └── gameday/            # Chaos engineering tests
│
├── schema/                  # Database schemas
│   └── clickhouse/         # ClickHouse table definitions
│
├── db/                      # Database backups and init scripts
│   ├── clickhouse/
│   └── pg/
│
├── docs/                    # Documentation and design specs
│   ├── strategies/         # Strategy specifications
│   ├── integration/        # System integration docs
│   └── system/             # Architecture documentation
│
└── scripts/                 # Operational scripts
    ├── scan_top_gainers.py
    ├── check_env.sh
    └── backup scripts
```

### 1.2 Key Entry Points

| File | Purpose |
|------|---------|
| `alpha_loop.py` | Main trading loop with AI-powered grid refinement |
| `paper_engine.py` | Paper trading simulator |
| `backtest_engine.py` | Backtesting framework |
| `demo_system.py` | Bundled simulation for dev/testing |
| `run_system.py` (inferred) | Live orchestration loop |
| `hpo_cli.py` | Hyperparameter optimization CLI |
| `hot_reload_test_params.py` | Dynamic parameter reloading |

### 1.3 Configuration Management

**Main Config Files:**
- `.env` - Environment variables (loads via python-dotenv)
- `config.yml` / `config.example.yml` - YAML configuration
- `libs/config.py` - Config singleton with property accessors

**Key Environment Categories:**
- Global runtime (TRADING_MODE, TRADING_SYMBOLS, LOG_LEVEL)
- PostgreSQL (PG_HOST, PG_PORT, PG_DB, etc.)
- ClickHouse (CH_HOST, CH_PORT, CH_DB, etc.)
- Redis (STATEBUS_REDIS)
- Kafka (KAFKA_BOOTSTRAP_SERVERS, SCHEMA_REGISTRY_URL)
- Interactive Brokers (IB_HOST, IB_PAPER_PORT, IB_LIVE_PORT, etc.)
- LLM providers (LLM_PROVIDER, LLM_MODEL, OpenAI/Anthropic keys)
- Health/monitoring thresholds (MAX_TIME_SKEW_SEC, L1_READY_TIMEOUT_SEC, etc.)

---

## 2. TECHNOLOGY STACK

### 2.1 Core Languages & Runtime
- **Python 3.13** - Primary language
- **asyncio** - Async runtime (no external event loop library)
- **Dataclasses** - For clean domain models

### 2.2 Key Frameworks & Libraries

#### Trading & Broker Integration
- **ib-insync==0.9.86** - Interactive Brokers API wrapper
- **eventkit==1.0.3** - Event system (via ib-insync)

#### Data & Analytics
- **pandas==2.3.3** - Time series and tabular data
- **numpy==2.3.4** - Numerical computing
- **xgboost==3.0.5** - Gradient boosting models
- **torch==2.9.0** - Deep learning framework
- **optuna==3.6.1** - Hyperparameter optimization

#### Database Clients
- **asyncpg==0.30.0** - PostgreSQL async client (primary)
- **clickhouse-connect==0.9.2** - ClickHouse HTTP client
- **redis==6.4.0** - Redis client
- **sqlalchemy==2.0.44** - ORM (used with alembic for migrations)

#### Async & Message Queue
- **aiohttp==3.9.1** - HTTP client/server
- **aiokafka==0.12.0** - Async Kafka client
- **confluent-kafka==2.12.0** - Confluent Kafka client
- **kafka-python==2.2.15** - Alternative Kafka client
- **avro-python3==1.10.2** - Avro serialization
- **fastavro==1.12.1** - Fast Avro serialization
- **lz4==4.4.4** - Compression (Kafka/ClickHouse)

#### Logging & Monitoring
- **structlog==24.1.0** - Structured logging (primary, JSON output)
- **prometheus-client==0.19.0** - Prometheus metrics
- **python-dateutil==2.9.0.post0** - Date utilities
- **pytz==2025.2** - Timezone handling

#### Utils & Configuration
- **python-dotenv==1.0.0** - .env file loading
- **pyyaml==6.0.1** - YAML parsing
- **requests==2.31.0** - HTTP requests
- **nest-asyncio==1.6.0** - Nested event loop support (ib-insync)

#### Testing
- **pytest==7.4.3** - Test framework
- **pytest-asyncio==0.21.1** - Async test support
- **pytest-mock==3.12.0** - Mocking support

### 2.3 Database Systems

#### ClickHouse (OLAP - Analytical)
- Purpose: Time-series bars, features, quotes, ticks
- Tables: `bars_5s`, `bars_1m`, `bars_5m`, `features_5s`, `quotes`, `ticks`
- Connection: HTTP via `clickhouse-connect`
- Pool size: 100 connections (configurable, patched in `libs/db/clickhouse.py`)
- Uses: Historical feature fetching, backtesting data, analytics
- Async support via `get_async_client()`

#### PostgreSQL (OLTP - Transactional)
- Purpose: Positions, orders, ledgers, portfolio state
- Key tables: `core_strategy_ledger`, `core_position`, `core_order`, `core_fill`
- Connection: asyncpg (async connection pool)
- Pool size: min=1, max=10 (configurable)
- Features: Transactions, stored procedures support
- Used for: Position tracking, order history, compliance audit

#### Redis
- Purpose: Real-time state bus (STATEBUS_REDIS)
- Role: Cache layer, pub/sub for events
- Config: `redis://127.0.0.1:6379/0` (default)

### 2.4 Async Patterns

**Design Philosophy:**
- Heavy use of `async/await` for I/O-bound operations
- `asyncio.create_task()` for fire-and-forget tasks
- Context managers (`@asynccontextmanager`) for transaction/resource management
- Task cancellation and cleanup on shutdown

**Key Async Services:**
- Market data polling (continuous bar ingestion)
- Order execution and monitoring
- Strategy execution loop
- Database operations (PostgreSQL, ClickHouse)

**Connection Pool Management:**
```python
# PostgreSQL: asyncpg with min/max connection limits
# ClickHouse: custom urllib3 pool patching for maxsize=100
# Both auto-connect on first operation if not already initialized
```

---

## 3. KEY COMPONENTS

### 3.1 Strategy Framework

**Architecture:**
- Strategies inherit from `StrategyAlgorithm` base class
- Provided context: `StrategyContext` with engine API
- Lifecycle: `initialize()` → `on_features()` → `on_shutdown()`
- Event-driven: Independent cadence per strategy (5s minimum)
- Decision model: `DecisionEnvelope` for trading intents

**Key Classes:**

```python
# services/strategy/strategy_base.py
class StrategyAlgorithm:
    async def initialize(self) -> None
    async def on_features(self, symbol: str, features: Dict) -> None
    async def on_bar(self, symbol: str, bar: Dict) -> None
    async def on_order_update(self, update: Dict) -> None
    async def on_position_update(self, position: Dict) -> None
    async def on_error(self, err: Exception, where: str) -> None
    async def on_shutdown(self) -> None

# Main execution engine
class StrategyEngine:
    async def load_strategy(strategy_id, name, params) -> None
    async def execute_strategy(strategy_id, symbol, features) -> None
    async def subscribe_features_unified(strategy_id, symbols, bar_sec) -> None
    async def history_features_unified(symbol, bar_type, limit) -> List
    async def run_backtest(strategy_id, symbol, start, end) -> Dict

# Real execution adapter
class RealEngine:
    async def submit_intent(strategy_id, envelope) -> None
    async def portfolio_view(strategy_id) -> Dict
    async def position_view(strategy_id, symbol) -> PositionSnapshot
    async def set_cadence(strategy_id, seconds) -> None
```

**Strategy Registration:**
```python
@register_strategy("premarket_momentum_v2")
class PremarketMomentumV2(StrategyAlgorithm):
    # ... implementation
```

**Example Strategy (Premarket Momentum V2):**
- Phase machine: PRE_SCAN → SCANNING → FINAL_DECISION → EXECUTE → MANAGE → CLOSED
- Scanning: Every 30 min (07:00-09:27 ET) for stocks with 3%+ premarket gains
- Decision: At 09:27, selects top candidate based on score (price momentum + duration + volume)
- Execution: 09:30 open, market order with dynamic limit pricing
- Management: Partial exits at +2%, full exit at +3% or -2% stop loss

### 3.2 Data Fetching & Processing

**Market Data Facade (`libs/dal/market_data_facade.py`):**
```python
class MarketDataFacade:
    async def bars_async(symbol, resolution, limit) -> List[Dict]
    async def last_quote_async(symbol, staleness_sec) -> Dict
    async def poll_new_bar(symbol, resolution, poll_sec, callback) -> None
    async def vix_symbol_async() -> str
```

**Data Flow:**
1. **Ingestion**: IBKR market data → Kafka → ClickHouse
2. **Feature Computation**: Raw bars + quotes → Features (VWAP, ATR, RSI, etc.)
3. **Strategy Access**: 
   - Historical: Direct ClickHouse query
   - Real-time: PriceCache + poll-based subscription
4. **Resolution Support**: 5s, 1m, 5m bars; configurable poll intervals

**VIX Handling:**
- Configurable VIX_ENABLED (default: true)
- Fallback: VIX_FALLBACK=18.0
- Aliasing: Strategies reference "VIX", system maps to actual symbol

### 3.3 Broker Integration (Interactive Brokers)

**Main Adapter:**
- `services/ibkr/` - IBKR connection management
- Uses: `ib-insync` wrapper
- Features:
  - Dual port support (paper: 4002, live: 4001)
  - Multiple client IDs for parallel services
  - Account and flex query support
  - Health monitoring

**Trading Modes:**
- `TRADING_MODE=shadow` - Shadow orders (logged, not sent to IBKR)
- `TRADING_MODE=paper` - Paper trading (simulated fills)
- `TRADING_MODE=live` - Live trading (actual orders)

**Order Submission Flow:**
1. Strategy generates `DecisionEnvelope` (target qty, limit price, TTL, etc.)
2. Decision pipeline validates (risk, compliance, quotas)
3. If approved, execution adapter sends to IBKR
4. Order tracking: Fills, partial fills, cancellations → PostgreSQL

### 3.4 Database Interactions

#### ClickHouse Usage Pattern
```python
# Connection pooling with patched urllib3
from libs.db.clickhouse import ClickHouseDB
db = ClickHouseDB()

# Queries with named parameters
sql = """
SELECT * FROM features_5s FINAL
WHERE symbol = {symbol:String}
  AND ts >= {start:DateTime}
  AND ts <= {end:DateTime}
ORDER BY ts
LIMIT {limit:UInt32}
"""
results = db.query(sql, {'symbol': 'AAPL', 'start': '2024-01-01', ...})

# Insert (sync or async)
db.insert('bars_5s', data, column_names=['ts_start', 'symbol', 'close', ...])
await db.insert_async('bars_5s', data)
```

#### PostgreSQL Usage Pattern
```python
# Async connection pool with transaction support
from libs.db.postgres import PostgresDB
pg = PostgresDB(min_conn=1, max_conn=10)
await pg.connect()

# Basic operations
result = await pg.fetchone("SELECT * FROM core_position WHERE symbol = $1", 'AAPL')
rows = await pg.fetchall("SELECT * FROM core_order WHERE state IN ('NEW', 'PARTIAL')")

# Transactions
async with pg.transaction():
    await pg.execute("INSERT INTO core_position ...", ...)
    await pg.execute("UPDATE core_ledger ...", ...)
```

### 3.5 Monitoring & Logging

**Structured Logging:**
- Framework: `structlog` with JSON output to files
- Console: Human-readable or JSON (configurable)
- File: Rotating handler (50MB max, 5 backups) for Promtail ingestion
- Path: `~/grafana-stack/logs/trader.log`
- Configuration: `libs/logger.py` with error hooks

**Observability Stack:**
- **Prometheus** - Metrics collection (Grafana dashboards)
- **Loki** - Log aggregation
- **Promtail** - Log shipper (tails JSON logs)
- **Tempo** - Distributed tracing
- **Grafana** - Visualization

**Key Metrics:**
```python
strategy_executions_total = Counter(
    'strategy_executions_total',
    'Total strategy executions',
    ['strategy_id', 'result']  # result: success|error
)

strategy_execution_time = Histogram(
    'strategy_execution_seconds',
    'Strategy execution time',
    ['strategy_id']
)
```

**Health Checks:**
- ClickHouse: `SELECT 1`
- PostgreSQL: `SELECT 1`
- IBKR: Quote availability and account status
- Configuration: `MAX_TIME_SKEW_SEC`, `L1_READY_TIMEOUT_SEC`, `L1_STALE_SEC`

---

## 4. TESTING INFRASTRUCTURE

### 4.1 Test Organization

```
tests/
├── e2e_exec/          # End-to-end execution tests
│   └── test_*.py
├── e2e_market/        # Market data ingestion tests
│   └── test_*.py
├── e2e_strategy/      # Strategy integration tests
│   └── test_*.py
└── gameday/           # Chaos/fault injection tests
    ├── fault_injection.py
    └── test_*.py
```

### 4.2 Testing Frameworks

- **pytest==7.4.3** - Core test runner
- **pytest-asyncio==0.21.1** - Async test support (`@pytest.mark.asyncio`)
- **pytest-mock==3.12.0** - Mocking utilities

### 4.3 Test Pattern Examples

**Example test file location:**
- Strategy tests → `tests/test_strategy*.py`
- Execution tests → `tests/e2e_exec/test_*.py`
- Market data tests → `tests/e2e_market/test_*.py`

**Running tests:**
```bash
pytest tests                    # Full suite
pytest tests -k keyword        # Filter by keyword
pytest tests -k "strategy"     # Strategy tests only
make test                      # Via Makefile
```

### 4.4 Testing Types

1. **Unit Tests**: Component-level (strategy logic, helpers)
2. **Integration Tests**: Service-to-service (strategy → decision → execution)
3. **End-to-End Tests**: Full workflows (market data → strategy → order → reconciliation)
4. **Chaos Tests**: Fault injection, recovery scenarios

---

## 5. CONFIGURATION & DEPENDENCIES

### 5.1 Dependency Management

**Files:**
- `requirements.txt` - Pinned versions (auto-generated from requirements.in)
- `requirements.in` - Source dependencies (high-level)
- Generated via: `pip-compile --output-file=requirements.txt requirements.in`

**Key Dependency Groups:**

| Group | Packages |
|-------|----------|
| Core | python-dotenv, pyyaml |
| Database | asyncpg, clickhouse-connect, redis |
| Kafka | kafka-python, confluent-kafka, aiokafka, avro, fastavro |
| IB Broker | ib-insync |
| Async | aiohttp, asyncio |
| Data | numpy, pandas, pytz, xgboost, torch |
| Monitoring | prometheus-client |
| Testing | pytest, pytest-asyncio, pytest-mock |
| Utils | requests, structlog |
| HPO | optuna |

### 5.2 Environment Configuration

**Hierarchy:**
1. `.env` file (git-ignored, local machine)
2. Environment variables (system level)
3. `config.yml` / `config.example.yml` (defaults)
4. Code defaults (if no config found)

**Key Categories:**

```
Global Runtime:
- TRADING_MODE: shadow | paper | live
- TRADING_SYMBOLS: comma-separated list
- TRADING_TZ: America/Vancouver
- LOG_LEVEL: DEBUG | INFO | WARNING | ERROR

Database:
- PG_*: PostgreSQL connection params
- CH_*: ClickHouse connection params
- STATEBUS_REDIS: Redis URL

Broker:
- ENV_MODE: paper | live
- IB_HOST, IB_PAPER_PORT, IB_LIVE_PORT
- IB_CLIENT_ID, IB_ACCOUNT, IB_USERNAME, IB_PASSWORD
- Multiple client IDs for parallel services

Feature Flags:
- VIX_ENABLED: true | false
- VIX_FALLBACK: 18.0
- MOCK_BROKER: true | false
- DEV_MODE: true | false

Monitoring:
- MAX_TIME_SKEW_SEC: 2.0
- L1_READY_TIMEOUT_SEC: 8
- HEALTH_RECHECK_MIN: 5
- L1_STALE_SEC: 60
```

---

## 6. COMMON PATTERNS

### 6.1 Error Handling

**Pattern 1: Try-Catch with Logging**
```python
try:
    result = await some_operation()
except asyncpg.PostgresError as e:
    logger.error("Database operation failed", error=str(e), exc_info=True)
    # Recovery or re-raise
except Exception as e:
    logger.error("Unexpected error", error=str(e), exc_info=True)
    raise
```

**Pattern 2: Async Context Manager for Cleanup**
```python
async with db.transaction():
    await db.execute("INSERT ...")
    await db.execute("UPDATE ...")
# Auto-rollback on exception, commit on success
```

**Pattern 3: Error Hooks for Uncaught Exceptions**
```python
# In main entry point
from libs.logger import install_error_hooks
install_error_hooks()  # Captures sys.excepthook, asyncio task errors, warnings
```

### 6.2 Logging Conventions

**Format:** Structured JSON with context binding
```python
from libs.logger import get_logger

logger = get_logger(__name__)

# Event logging
logger.info("Strategy loaded", strategy_id=strategy_id, name=name, cadence_s=5)
logger.warning("Slow execution", strategy_id=strategy_id, elapsed_ms=250)
logger.error("Order failed", order_id=order_id, error=str(e), exc_info=True)

# Context binding
logger = logger.bind(component="strategy_engine", version="1.0")
logger.info("Engine initialized")
```

### 6.3 Code Organization Patterns

**Domain-Driven Design:**
- Each domain (strategy, execution, risk) is a separate service
- Shared utilities in `libs/`
- Clear boundaries and interfaces between services

**Dependency Injection:**
```python
class StrategyEngine:
    def __init__(self, market_data_facade=None, decision_pipeline=None, price_cache=None):
        self.md = market_data_facade or MarketDataFacade()
        self.decision_pipeline = decision_pipeline
        self.price_cache = price_cache
```

**Configuration via Environment:**
```python
# In config.py
@property
def pg_config(self) -> Dict[str, Any]:
    return {
        'host': os.getenv('PG_HOST', 'localhost'),
        'port': int(os.getenv('PG_PORT', 5432)),
        # ...
    }
```

**Registry Pattern for Strategies:**
```python
# In strategy module
@register_strategy("premarket_momentum_v2")
class PremarketMomentumV2(StrategyAlgorithm):
    pass

# At runtime
strategy = registry.load("premarket_momentum_v2", params, context)
```

### 6.4 API/Interface Conventions

**Strategy API (What strategies call):**
```python
# Data access
await ctx.subscribe_features(symbols=['AAPL'], bar_sec=5)
history = await ctx.history(symbol='AAPL', bar_sec=60, limit=100)

# Portfolio/position access
portfolio = await ctx.portfolio()
position = await ctx.position('AAPL')

# Configuration
ctx.param('max_notional', default=100000)
ctx.now_ms()  # Current timestamp in milliseconds

# Real-time prices
price = ctx.get_price('AAPL')  # Returns PriceSnapshot(bid, ask, last)
stats = ctx.get_minute_stats('AAPL')  # Returns MinuteRollup (VWAP, RVOL, etc.)

# Order submission
await ctx.submit_intent(DecisionEnvelope(...))

# Cadence control
await ctx.set_cadence(seconds=10)
```

**Decision Intent Format:**
```python
DecisionEnvelope(
    decision_id=str,           # Unique ID
    strategy_id=str,           # Which strategy
    symbol=str,                # Trading symbol
    ts_bar_end_us=int,        # Bar end timestamp (microseconds)
    target_qty=float,          # Target position (positive=long, negative=short)
    order_style=OrderStyle(    # Execution instruction
        kind='MKT'|'LMT'|'STP_LMT',
        limit_px=Optional[float],
        stop_px=Optional[float]
    ),
    freshness=Freshness(       # TTL configuration
        ttl_ms=int,            # How long the intent is valid
        dlb_ms=int             # Decision lag budget
    ),
    extras=Dict[str, Any]      # Additional context (ref_px, rationale, etc.)
)
```

### 6.5 Async Patterns

**Pattern 1: Fire-and-Forget Tasks**
```python
task = asyncio.create_task(self.md.poll_new_bar(...))
self._poll_tasks.append(task)  # Track for cleanup
```

**Pattern 2: Task Cancellation on Shutdown**
```python
async def shutdown(self):
    for task in self._poll_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
```

**Pattern 3: Lazy Connection Initialization**
```python
async def execute(self, query):
    if not self.pool:
        await self.connect()  # Auto-connect on first use
    async with self.pool.acquire() as conn:
        return await conn.execute(query)
```

---

## 7. CRITICAL AREAS FOR CODE QUALITY & BUG PREVENTION

### 7.1 High-Risk Zones

1. **Connection Pool Management**
   - ClickHouse: Custom urllib3 patching for maxsize=100
   - PostgreSQL: asyncpg pool lifecycle management
   - Issue: Pool exhaustion, lingering connections → deadlocks
   - Prevention: Always use context managers, verify pool settings

2. **Async Task Management**
   - Long-running poll tasks for market data
   - Task cancellation on shutdown
   - Issue: Uncancelled tasks → process hangs
   - Prevention: Track all long-lived tasks, cancel on shutdown

3. **State Synchronization**
   - Strategy state (candidates, positions) across phases
   - Position tracking in PostgreSQL vs. actual fills
   - Issue: Race conditions, divergence between actual and recorded state
   - Prevention: Use transactions, atomic updates, reconciliation

4. **Timestamp & Timezone Handling**
   - Market hours (ET timezone)
   - Bar aggregation windows
   - Database storage (UTC vs. session timezone)
   - Issue: Off-by-one bars, missed market sessions
   - Prevention: Always use explicit ZoneInfo, document assumptions

5. **Order Execution Guarantees**
   - Shadow mode vs. live mode switch
   - Decision pipeline approval logic
   - Issue: Duplicate orders, missed fills, orphaned orders
   - Prevention: Idempotency keys, decision audit trail, reconciliation

### 7.2 Testing Recommendations

- Unit test strategy logic independently (mock context)
- Integration test: strategy → decision pipeline → execution
- E2E test: market data → strategy → order flow
- Chaos test: network failures, slow databases, missing quotes
- Load test: Multiple strategies, high cadence

### 7.3 Code Review Checklist

When reviewing changes:

- [ ] Database operations use connection pools (no raw connections)
- [ ] Async tasks are tracked and properly cancelled on shutdown
- [ ] Timestamps use explicit timezone (ZoneInfo, not naive)
- [ ] Errors logged with context (strategy_id, symbol, error)
- [ ] Configuration loaded from .env or config.yml, not hardcoded
- [ ] Strategy logic has time budget <200ms (P95)
- [ ] Transactions used for multi-step operations
- [ ] Tests added for new logic (unit + integration)
- [ ] No secrets committed (IB credentials, API keys)
- [ ] Logging format consistent (structlog JSON)

---

## 8. DEPLOYMENT & OPERATIONS

### 8.1 Infrastructure

**Docker Compose Stack:**
```yaml
Services:
- ClickHouse (8123 HTTP, 9000 native)
- PostgreSQL (5432)
- Redis (6379)
- Kafka + Schema Registry
- Prometheus (9090)
- Grafana (3000)
- Loki (3100)
- Promtail (log shipper)
- Tempo (tracing)
```

**Commands:**
```bash
make up                          # Start all services
make down                        # Stop all services
make verify                      # Health check
make test                        # Run tests
make backup-pg / make backup-ch  # Backup databases
make recon-all                   # Data reconciliation
```

### 8.2 Key Operational Files

- `Makefile` - Build and deploy automation
- `scripts/check_env.sh` - Environment validation
- `infra/docker-compose.yml` - Service orchestration
- `.env` - Local configuration (git-ignored)

---

## 9. SUMMARY: KEY TAKEAWAYS

| Aspect | Key Point |
|--------|-----------|
| **Language** | Python 3.13 with heavy async/await |
| **Execution Model** | Event-driven, independent strategy cadences |
| **Databases** | ClickHouse (OLAP), PostgreSQL (OLTP), Redis (cache) |
| **Broker** | Interactive Brokers via ib-insync |
| **Framework** | Custom strategy engine with plugin pattern |
| **Logging** | Structured JSON via structlog + Promtail → Loki |
| **Monitoring** | Prometheus + Grafana dashboards |
| **Trading Modes** | Shadow (logged only), Paper (simulated), Live (real) |
| **Config** | Env-driven (12-factor) with YAML fallback |
| **Testing** | pytest + asyncio, with e2e and chaos tests |
| **Deployment** | Docker Compose with Makefile automation |

This is a production-grade, well-architected system with clear separation of concerns, async-first design, and operational tooling. The main risk areas are around connection pool management, async task lifecycle, and state synchronization between components.

