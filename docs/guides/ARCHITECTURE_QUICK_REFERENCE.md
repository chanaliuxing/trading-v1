# Trading System - Architecture Quick Reference

## File Locations

### Core Services
- **Strategy Engine**: `/services/strategy/engine.py`, `strategy_base.py`
- **Execution**: `/services/exec/` 
- **Decision Pipeline**: `/services/decision/`
- **Risk Management**: `/services/risk/`
- **IBKR Adapter**: `/services/ibkr/`

### Shared Libraries
- **Database Clients**: `/libs/db/clickhouse.py`, `postgres.py`
- **Data Access Layer**: `/libs/dal/market_data_facade.py`
- **Logging**: `/libs/logger.py`
- **Configuration**: `/libs/config.py`
- **Cache**: `/libs/cache/price_cache.py`
- **Strategy Base**: `/libs/strategy/`

### Strategies
- **Premarket Momentum**: `/strategies/premarket_momentum_v2.py`
- **TSLA Specific**: `/strategies/tsla_v7_2.py`
- **Model Signals**: `/strategies/model_signal_strategy.py`

### Infrastructure
- **Docker Compose**: `/infra/docker-compose.yml`
- **ClickHouse Config**: `/infra/clickhouse-config/`
- **Prometheus**: `/infra/prometheus/`
- **Makefile**: `/Makefile`

---

## Database Quick Reference

### ClickHouse (OLAP)
```python
from libs.db.clickhouse import ClickHouseDB
db = ClickHouseDB()

# Query
sql = "SELECT * FROM bars_5s WHERE symbol = {symbol:String}"
results = db.query(sql, {'symbol': 'AAPL'})

# Insert
db.insert('bars_5s', data, column_names=['ts_start', 'symbol', 'close'])

# Async
await db.insert_async('bars_5s', data)
```

### PostgreSQL (OLTP)
```python
from libs.db.postgres import PostgresDB
pg = PostgresDB()
await pg.connect()

# Query
result = await pg.fetchone("SELECT * FROM core_position WHERE symbol = $1", 'AAPL')
rows = await pg.fetchall("SELECT * FROM core_order WHERE state = $1", 'NEW')

# Transaction
async with pg.transaction():
    await pg.execute("INSERT INTO core_order ...")
    await pg.execute("UPDATE core_position ...")
```

---

## Configuration

### Environment Variables (Key)
```bash
TRADING_MODE=shadow|paper|live           # Execution mode
TRADING_SYMBOLS=AAPL,MSFT,TSLA          # Comma-separated
TRADING_TZ=America/New York             # Timezone
LOG_LEVEL=INFO                           # Debug level

# Database
PG_HOST=localhost PG_PORT=5432 PG_DB=test PG_USER=postgres
CH_HOST=localhost CH_PORT=8123 CH_DB=trading

# Broker
ENV_MODE=paper
IB_HOST=127.0.0.1 IB_PAPER_PORT=4002 IB_LIVE_PORT=4001
IB_CLIENT_ID=7
```

### Load Configuration
```python
from libs.config import config

pg_cfg = config.pg_config  # Dict with host, port, user, password
ch_cfg = config.clickhouse_config
ib_cfg = config.ib_config
```

---

## Strategy Development

### Basic Template
```python
from services.strategy.strategy_base import StrategyAlgorithm, DecisionEnvelope
from libs.strategy.registry import register_strategy
from libs.logger import get_logger

logger = get_logger(__name__)

@register_strategy("my_strategy_v1")
class MyStrategy(StrategyAlgorithm):
    async def initialize(self):
        """One-time setup"""
        await self.ctx.subscribe_features(['AAPL', 'MSFT'], bar_sec=60)
    
    async def on_features(self, symbol: str, features: dict):
        """Main logic - called on each bar"""
        close = features.get('close')
        zscore = features.get('zscore_20')
        
        if zscore < -2.0:
            # Generate trading intent
            envelope = DecisionEnvelope(
                decision_id=make_decision_id(),
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                target_qty=100.0,  # shares
                order_style=OrderStyle(kind='MKT'),
                freshness=Freshness(ttl_ms=5000, dlb_ms=500)
            )
            await self.ctx.submit_intent(envelope)
    
    async def on_error(self, err: Exception, where: str):
        """Handle errors"""
        logger.error(f"Error in {where}", error=str(err))
    
    async def on_shutdown(self):
        """Cleanup"""
        logger.info("Strategy shutting down")
```

### Strategy Context API
```python
# Data
await ctx.subscribe_features(symbols=['AAPL'], bar_sec=5)
history = await ctx.history(symbol='AAPL', bar_sec=60, limit=100)
price = ctx.get_price('AAPL')  # Real-time L1 snapshot
minute_stats = ctx.get_minute_stats('AAPL')  # VWAP, RVOL, etc.

# Portfolio
portfolio = await ctx.portfolio()  # Cash, equity, positions
position = await ctx.position('AAPL')  # Qty, avg_px, pnl

# Parameters
val = ctx.param('my_param', default=100)
ts_ms = ctx.now_ms()
ts_us = ctx.now_us()

# Execution
await ctx.submit_intent(envelope)
await ctx.set_cadence(seconds=10)
```

---

## Key Classes & Data Structures

### StrategyAlgorithm Lifecycle
```python
class StrategyAlgorithm:
    # Called once at startup
    async def initialize(self) -> None
    
    # Called on each feature update (time budget <200ms)
    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None
    
    # Called on raw bar data (optional)
    async def on_bar(self, symbol: str, bar: Dict[str, Any]) -> None
    
    # Called on order status changes
    async def on_order_update(self, update: Dict[str, Any]) -> None
    
    # Called on position changes
    async def on_position_update(self, position: Dict[str, Any]) -> None
    
    # Called on error in any handler
    async def on_error(self, err: Exception, where: str) -> None
    
    # Called on shutdown
    async def on_shutdown(self) -> None
```

### DecisionEnvelope (Trading Intent)
```python
@dataclass
class DecisionEnvelope:
    decision_id: str              # Unique ID
    strategy_id: str              # Strategy identifier
    symbol: str                   # Trading symbol
    ts_bar_end_us: int           # Bar end timestamp (microseconds)
    target_qty: float            # Target position
    order_style: OrderStyle(      # Execution style
        kind='MKT'|'LMT'|'STP_LMT',
        limit_px=Optional[float],
        stop_px=Optional[float]
    )
    freshness: Freshness(         # TTL configuration
        ttl_ms=int,              # Time-to-live
        dlb_ms=int               # Decision lag budget
    )
    extras: Dict[str, Any]        # Additional context
```

---

## Logging

### Basic Usage
```python
from libs.logger import get_logger

logger = get_logger(__name__)

# Event logging with context
logger.info("Strategy started", strategy_id="strat_1", symbol="AAPL")
logger.warning("Slow execution", elapsed_ms=250)
logger.error("Order failed", order_id="ord_123", error=str(e), exc_info=True)

# Context binding
logger = logger.bind(component="engine", version="1.0")
logger.info("Engine initialized")
```

### File Output
- Location: `~/grafana-stack/logs/trader.log`
- Format: JSON (for Promtail ingestion)
- Rotation: 50MB max, 5 backups

---

## Common Operations

### Start Services
```bash
make up            # Start all infra (Docker Compose)
make verify        # Health check all services
make logs          # Tail logs
```

### Testing
```bash
make test                           # Full test suite
pytest tests -k strategy           # Strategy tests only
pytest tests/e2e_exec              # Execution e2e tests
```

### Backups
```bash
make backup-pg                     # PostgreSQL backup
make backup-ch                     # ClickHouse backup
make backup-ch-full                # Full ClickHouse snapshot
```

### Database Initialization
```bash
make ch-init-wf-state              # Create checkpoint table
make ch-init-15s                   # Create 15s bars
make ch-init-vix                   # Create VIX table
```

---

## Health & Monitoring

### Key Metrics (Prometheus)
```python
strategy_executions_total          # Counter: total executions
strategy_execution_seconds         # Histogram: execution time
```

### Health Checks
```python
# ClickHouse
db.health_check() -> bool

# PostgreSQL
await pg.health_check() -> bool

# Configuration
MAX_TIME_SKEW_SEC=2.0
L1_READY_TIMEOUT_SEC=8
HEALTH_RECHECK_MIN=5
L1_STALE_SEC=60
```

### Dashboards
- Grafana: http://localhost:3000
- Prometheus: http://localhost:9090
- Loki: http://localhost:3100

---

## Critical Patterns

### Don't Do This
```python
# Wrong: Creates new connection each time
import asyncpg
conn = await asyncpg.connect(...)  # Resource leak!

# Wrong: Ignores timezone
import datetime
now = datetime.datetime.now()  # Naive datetime, can cause bugs

# Wrong: Untracked task
asyncio.create_task(some_coro())  # Untracked, may hang on shutdown
```

### Do This Instead
```python
# Right: Use connection pool
from libs.db.postgres import PostgresDB
pg = PostgresDB()
await pg.connect()
await pg.execute("SELECT ...")

# Right: Explicit timezone
from zoneinfo import ZoneInfo
now = datetime.datetime.now(ZoneInfo("America/New_York"))

# Right: Track tasks for cleanup
task = asyncio.create_task(some_coro())
self._poll_tasks.append(task)
# ... on shutdown:
for task in self._poll_tasks:
    task.cancel()
```

---

## Troubleshooting

### Services Won't Connect
1. Check `.env` file exists and is loaded
2. Verify Docker containers running: `docker ps`
3. Check logs: `make logs`
4. Verify health: `make verify`

### Slow Queries
1. Check ClickHouse pool size (default 100)
2. Check PostgreSQL pool size (default min=1, max=10)
3. Check network latency to databases

### Strategy Not Executing
1. Verify strategy registered: `@register_strategy("name")`
2. Check strategy initialized: `await engine.load_strategy(...)`
3. Verify features subscribed: `await ctx.subscribe_features(...)`
4. Check logs for errors: `grep strategy ~/grafana-stack/logs/trader.log`

### Database Errors
1. Check connection pools not exhausted
2. Verify transactions closed properly
3. Check for deadlocks: `SELECT * FROM core.locks;`
4. Review error logs for specific errors

---

## Documentation References

- **Architecture**: `/doc/system/`
- **Strategy Specs**: `/doc/strategies/`
- **Integration**: `/doc/integration/`
- **Backtest Guide**: `/BACKTEST_GUIDE.md`
- **Async Migration**: `/ASYNCPG_MIGRATION_COMPLETE.md`
- **Agent Guidelines**: `/AGENTS.md`

