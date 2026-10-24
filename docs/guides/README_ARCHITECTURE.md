# Trading System - Comprehensive Architecture Overview

This document provides a complete guide to understanding and working with the trading system codebase.

## Quick Navigation

1. **Full Architecture Analysis**: See `CODEBASE_ANALYSIS.md` (783 lines)
   - Detailed breakdown of all components
   - Technology stack deep dive
   - Common patterns and best practices
   - Critical areas for code quality

2. **Quick Reference**: See `ARCHITECTURE_QUICK_REFERENCE.md`
   - File locations and key services
   - Copy-paste code examples
   - Common operations
   - Troubleshooting guide

3. **Code Guidelines**: See `AGENTS.md`
   - Coding style and conventions
   - Commit and PR guidelines
   - Testing guidelines
   - Security tips

---

## System at a Glance

### What This System Does
- **Trading Platform**: Executes quantitative strategies across multiple instruments
- **Modes**: Shadow (testing), Paper (simulated), Live (real money)
- **Timeframes**: Supports intraday trading with 5-second resolution bars
- **Broker**: Uses Interactive Brokers for order execution
- **Strategies**: Plugin architecture for strategy development

### Architecture Layers

```
┌─────────────────────────────────────────────────────────┐
│  User Strategy Layer (strategies/*)                     │
│  - Premarket Momentum                                   │
│  - TSLA Specific                                        │
│  - Model Signal                                         │
└────────────────┬────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────┐
│  Strategy Engine (services/strategy/engine.py)          │
│  - Load/execute strategies                              │
│  - Feature subscription                                 │
│  - Independent cadences                                 │
└────────────────┬────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────┐
│  Decision Pipeline (services/decision/)                 │
│  - Validate trading intents                             │
│  - Risk checks, compliance                              │
│  - Arbiter logic                                        │
└────────────────┬────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────┐
│  Execution Layer (services/exec/)                       │
│  - Order submission to IBKR                             │
│  - Fill tracking, reconciliation                        │
└────────────────┬────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────┐
│  Data Layer                                             │
│  ┌──────────────────────┬───────────────────────────┐   │
│  │ ClickHouse (OLAP)    │  PostgreSQL (OLTP)        │   │
│  │ - Bars/features      │  - Positions/orders       │   │
│  │ - Historical data    │  - Ledgers/fills          │   │
│  │ - Analytics          │  - Compliance audit       │   │
│  └──────────────────────┴───────────────────────────┘   │
│                                                         │
│  Redis: State bus, cache layer                          │
│  Kafka: Market data ingestion                           │
└─────────────────────────────────────────────────────────┘
```

---

## Key Responsibilities

### Strategy Engine (`services/strategy/engine.py`)
- Loads and manages strategy lifecycle
- Polls ClickHouse for new bars
- Triggers strategy callbacks with features
- Tracks independent execution cadences
- Forwards trading intents to decision pipeline

### Real Engine (`services/strategy/engine.py`)
- Adapts strategies to external services
- Manages portfolio/position views
- Submits intents to decision pipeline
- Logs shadow orders for audit

### Market Data Facade (`libs/dal/market_data_facade.py`)
- Unified interface to market data
- Fetches historical bars/features
- Polls for new bars on schedule
- Handles VIX aliasing

### Decision Pipeline (`services/decision/`)
- Validates trading intents
- Applies risk limits
- Checks compliance rules
- Logs decisions for audit

### Order Execution (`services/exec/`)
- Submits approved orders to IBKR
- Tracks fills and partial fills
- Reconciles with IBKR positions
- Maintains order audit trail

---

## Technology Stack Summary

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.13 |
| **Async Runtime** | asyncio (built-in) |
| **OLAP DB** | ClickHouse (HTTP client) |
| **OLTP DB** | PostgreSQL (asyncpg) |
| **Broker** | Interactive Brokers (ib-insync) |
| **Messaging** | Kafka (confluent-kafka) |
| **Logging** | structlog + JSON |
| **Monitoring** | Prometheus + Grafana |
| **Testing** | pytest + asyncio |
| **Config** | python-dotenv + YAML |

---

## Configuration Hierarchy

1. **`.env` file** (highest priority, git-ignored)
2. **System environment variables**
3. **`config.yml`** (checked first for config values)
4. **Code defaults** (lowest priority)

**Key Environment Variables:**
```bash
TRADING_MODE=shadow|paper|live       # Execution mode
TRADING_SYMBOLS=AAPL,MSFT           # Comma-separated
TRADING_TZ=America/Vancouver        # Timezone
LOG_LEVEL=INFO                      # Debug level
PG_HOST, PG_PORT, PG_DB, PG_USER   # PostgreSQL config
CH_HOST, CH_PORT, CH_DB            # ClickHouse config
IB_HOST, IB_PAPER_PORT, IB_LIVE_PORT, IB_CLIENT_ID  # Broker config
```

---

## Common Development Tasks

### Add a New Strategy
1. Create file in `/strategies/` (e.g., `my_strategy_v1.py`)
2. Inherit from `StrategyAlgorithm`
3. Implement lifecycle: `initialize()`, `on_features()`, `on_shutdown()`
4. Register with `@register_strategy("my_strategy_v1")`
5. Add config file in `/strategies/configs/my_strategy_v1.json`

### Run Tests
```bash
make test                           # Full suite
pytest tests -k strategy            # Strategy tests only
pytest tests/e2e_exec               # Execution tests
```

### Debug Strategy Logic
1. Add logging: `logger.info("Debug info", key=value)`
2. Check logs: `grep strategy ~/grafana-stack/logs/trader.log`
3. Run backtest for historical replay
4. Use shadow mode for paper trading

### Database Query
```python
from libs.db.clickhouse import ClickHouseDB
from libs.db.postgres import PostgresDB

# ClickHouse
ch = ClickHouseDB()
bars = ch.query("SELECT * FROM bars_5s WHERE symbol = {s:String}", {'s': 'AAPL'})

# PostgreSQL (async)
pg = PostgresDB()
await pg.connect()
positions = await pg.fetchall("SELECT * FROM core_position")
```

---

## Critical Areas to Watch

### Connection Pool Management
- **Risk**: Pool exhaustion → deadlocks
- **Prevention**: Always use connection managers, verify pool sizes
- **Files**: `libs/db/clickhouse.py`, `libs/db/postgres.py`

### Async Task Lifecycle
- **Risk**: Untracked tasks → process hangs on shutdown
- **Prevention**: Track all long-lived tasks, cancel on shutdown
- **Files**: `services/strategy/engine.py`, strategy implementations

### Timestamp Handling
- **Risk**: Timezone mistakes → off-by-one bars, missed sessions
- **Prevention**: Always use explicit ZoneInfo ("America/New_York")
- **Files**: `strategies/premarket_momentum_v2.py` (good example)

### State Synchronization
- **Risk**: Divergence between DB state and actual broker positions
- **Prevention**: Use transactions, atomic updates, reconciliation
- **Files**: `services/exec/`, `services/reconciliation/`

### Order Execution Guarantees
- **Risk**: Duplicate orders, orphaned fills
- **Prevention**: Idempotency keys, decision audit trail
- **Files**: Decision pipeline, execution adapter

---

## Operational Commands

### Infrastructure
```bash
make up              # Start all services
make down            # Stop all services
make verify          # Health check
make logs            # Tail logs
```

### Maintenance
```bash
make backup-pg       # PostgreSQL backup
make backup-ch       # ClickHouse backup
make test            # Run test suite
```

### Database
```bash
make ch-init-wf-state   # Create checkpoint table
make ch-init-15s        # Create 15s bar resolution
make ch-init-vix        # Create VIX reference table
```

---

## Code Review Checklist

Before merging code, verify:

- [ ] Database operations use connection pools (never raw connections)
- [ ] All async tasks tracked and properly cancelled on shutdown
- [ ] Timestamps use explicit timezone (ZoneInfo, not naive)
- [ ] Errors logged with full context (strategy_id, symbol, etc.)
- [ ] Configuration loaded from .env or config.yml (no hardcoding)
- [ ] Strategy logic time budget <200ms (P95)
- [ ] Multi-step operations use transactions
- [ ] New logic covered by unit + integration tests
- [ ] No secrets committed (credentials, API keys)
- [ ] Logging format consistent (structlog JSON)

---

## File Organization Rationale

```
libs/                 # Reusable across all domains
├─ db/               # Database clients (connection pools)
├─ strategy/         # Strategy framework + base classes
├─ dal/              # Data access abstractions
└─ logger.py         # Centralized logging

services/            # Domain-specific services (independently deployable)
├─ strategy/         # Strategy execution
├─ exec/             # Order execution
├─ decision/         # Trading decision validation
├─ risk/             # Risk limits and compliance
└─ ...               # Other domains

strategies/          # User strategy implementations (plugin pattern)
├─ premarket_momentum_v2.py
├─ tsla_v7_2.py
└─ configs/          # Strategy configuration files

tests/               # Mirrors runtime structure
├─ e2e_exec/
├─ e2e_strategy/
└─ gameday/          # Chaos/fault injection tests

infra/               # Infrastructure as code
├─ docker-compose.yml
├─ clickhouse-config/
└─ prometheus/
```

---

## Documentation Structure

- **`CODEBASE_ANALYSIS.md`**: Complete breakdown of architecture, technology, patterns
- **`ARCHITECTURE_QUICK_REFERENCE.md`**: Quick lookup for common tasks and code patterns
- **`AGENTS.md`**: Guidelines for developers (style, commits, testing)
- **`doc/system/`**: System design specs
- **`doc/strategies/`**: Strategy specifications
- **`doc/integration/`**: Cross-service integration docs

---

## Performance Considerations

### Database
- ClickHouse: Custom urllib3 pool (100 connections)
- PostgreSQL: asyncpg pool (min=1, max=10)
- Both auto-connect on first use

### Strategy Execution
- Time budget: <200ms per feature update (P95)
- Cadence: Configurable per strategy (minimum 5s)
- Polling: Configurable intervals for new bar detection

### Network
- IBKR connection: Persistent with dual port support
- Kafka: Compression enabled (LZ4)
- ClickHouse: HTTP client (can be optimized)

---

## Deployment Checklist

Before production deployment:

1. [ ] All environment variables configured (`.env` file)
2. [ ] Database backups tested and working
3. [ ] Monitoring dashboards configured (Grafana)
4. [ ] Alerts configured (Prometheus/PagerDuty)
5. [ ] Strategy backtested and validated
6. [ ] Paper trading completed successfully
7. [ ] Risk limits configured and verified
8. [ ] IBKR account and credentials verified
9. [ ] Trading mode explicitly set to `shadow` or `paper` before `live`
10. [ ] All tests passing

---

## Getting Help

### Common Issues

**Strategy not executing?**
- Check strategy registered: `@register_strategy("name")`
- Verify initialized: `await engine.load_strategy(...)`
- Check features subscribed: `await ctx.subscribe_features(...)`
- Look at logs: `grep strategy ~/grafana-stack/logs/trader.log`

**Database connection errors?**
- Verify `.env` file exists and is loaded
- Check Docker containers: `docker ps`
- Verify health: `make verify`

**Order not submitting?**
- Check trading mode (shadow/paper/live)
- Verify decision pipeline enabled
- Check risk pipeline logs
- Verify IBKR connection and credentials

**Slow performance?**
- Check pool sizes (ClickHouse 100, PostgreSQL 10)
- Review query execution times
- Check network latency
- Profile strategy execution time

---

## Next Steps

1. **Start here**: Read `ARCHITECTURE_QUICK_REFERENCE.md` for quick reference
2. **Deep dive**: Read `CODEBASE_ANALYSIS.md` for comprehensive understanding
3. **Code guide**: Read `AGENTS.md` for development guidelines
4. **Try it**: Write a simple strategy using the template
5. **Run tests**: Execute `make test` to verify setup
6. **Deploy**: Follow the deployment checklist

---

## Key Files to Know

| File | Purpose |
|------|---------|
| `libs/logger.py` | Structured logging setup |
| `libs/config.py` | Configuration singleton |
| `libs/db/clickhouse.py` | ClickHouse client (OLAP) |
| `libs/db/postgres.py` | PostgreSQL client (OLTP) |
| `services/strategy/engine.py` | Strategy execution engine |
| `services/strategy/strategy_base.py` | Strategy base class |
| `services/decision/pipeline.py` | Decision validation |
| `strategies/premarket_momentum_v2.py` | Example strategy |
| `Makefile` | Build and deploy automation |
| `.env.example` | Environment template |

---

Generated: October 17, 2025
System Version: Trading Platform v1.0
Last Updated: October 16, 2024
