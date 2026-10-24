# Trading System Documentation Index

## Overview

This directory now contains comprehensive documentation about the trading system architecture. Use this index to find what you need.

---

## Documentation Files (Newest)

### 1. README_ARCHITECTURE.md (14 KB)
**Start here** - Provides the quickest path to understanding the system

**Contents:**
- System at a glance (what it does, architecture layers)
- Key responsibilities of each component
- Technology stack summary
- Configuration hierarchy
- Common development tasks
- Critical areas to watch
- Code review checklist
- Troubleshooting guide

**Best for:** Getting oriented, onboarding, quick overview

---

### 2. CODEBASE_ANALYSIS.md (26 KB)
**The comprehensive reference** - Complete architectural analysis

**Contents:**
- Complete project architecture (1.1)
- Core directory structure with detailed explanations
- Key entry points (1.2)
- Configuration management (1.3)
- Technology stack (Section 2)
  - Languages and runtime
  - Frameworks and libraries (organized by category)
  - Database systems (ClickHouse, PostgreSQL, Redis)
  - Async patterns
- Key components (Section 3)
  - Strategy framework with lifecycle
  - Data fetching and processing
  - Broker integration (IBKR)
  - Database interactions with code examples
  - Monitoring and logging
- Testing infrastructure (Section 4)
- Configuration and dependencies (Section 5)
- Common patterns (Section 6)
  - Error handling (3 patterns)
  - Logging conventions
  - Code organization patterns
  - API/interface conventions
  - Async patterns
- Critical areas for code quality (Section 7)
  - High-risk zones with prevention strategies
  - Testing recommendations
  - Code review checklist
- Deployment and operations (Section 8)
- Summary and key takeaways (Section 9)

**Best for:** Deep understanding, reference, architecture decisions, training

---

### 3. ARCHITECTURE_QUICK_REFERENCE.md (10 KB)
**Copy-paste ready** - Quick reference with code examples

**Contents:**
- File locations (core services, shared libraries, strategies, infrastructure)
- Database quick reference (ClickHouse and PostgreSQL with code)
- Configuration (environment variables and loading)
- Strategy development (basic template with lifecycle API)
- Key classes and data structures
- Logging usage examples
- Common operations (start services, testing, backups)
- Health and monitoring
- Critical patterns (do's and don'ts)
- Troubleshooting FAQ

**Best for:** Quick lookups, copy-paste code, operational commands

---

## Existing Documentation

### AGENTS.md (2 KB)
**Developer guidelines** - Code style and conventions

**Contents:**
- Project structure and module organization
- Build, test, and development commands
- Coding style and naming conventions
- Testing guidelines
- Commit and PR guidelines
- Security and configuration tips

**Best for:** Understanding coding standards, preparing to contribute

---

### BACKTEST_GUIDE.md
**Backtesting documentation** - How to run backtests

**Best for:** Backtesting strategies, understanding backtest engine

---

### ASYNCPG_MIGRATION_COMPLETE.md
**Database async migration** - Migration from sync to async PostgreSQL

**Best for:** Understanding asyncpg implementation details

---

## Directory Structure

```
trading/
├── README_ARCHITECTURE.md               <- START HERE (overview)
├── CODEBASE_ANALYSIS.md                 <- Deep dive (comprehensive)
├── ARCHITECTURE_QUICK_REFERENCE.md      <- Copy-paste reference
├── AGENTS.md                            <- Developer guidelines
├── DOCUMENTATION_INDEX.md               <- This file
│
├── libs/                                <- Shared utilities
│   ├── db/
│   │   ├── clickhouse.py               <- ClickHouse client
│   │   └── postgres.py                 <- PostgreSQL async client
│   ├── strategy/                        <- Strategy framework
│   ├── dal/
│   │   └── market_data_facade.py       <- Market data API
│   ├── logger.py                        <- Structured logging
│   └── config.py                        <- Configuration singleton
│
├── services/                            <- Domain services
│   ├── strategy/
│   │   ├── engine.py                   <- StrategyEngine + RealEngine
│   │   ├── strategy_base.py            <- StrategyAlgorithm base class
│   │   └── rule_engine.py              <- Rule execution logic
│   ├── exec/                            <- Order execution
│   ├── decision/                        <- Decision pipeline
│   ├── risk/                            <- Risk management
│   ├── ibkr/                            <- IBKR adapter
│   └── ...                              <- Other services
│
├── strategies/                          <- User strategy implementations
│   ├── premarket_momentum_v2.py         <- Example: Premarket scanning
│   ├── tsla_v7_2.py                    <- Example: TSLA specific
│   ├── aapl_dividend_wave.py           <- Example: Event-based
│   └── configs/                         <- Strategy configurations
│
├── tests/                               <- Test suite
│   ├── e2e_exec/                        <- End-to-end execution
│   ├── e2e_market/                      <- End-to-end market data
│   ├── e2e_strategy/                    <- End-to-end strategy
│   └── gameday/                         <- Chaos/fault tests
│
├── infra/                               <- Infrastructure
│   ├── docker-compose.yml               <- Service orchestration
│   ├── clickhouse-config/               <- ClickHouse config
│   └── prometheus/                      <- Prometheus setup
│
├── Makefile                             <- Build automation
└── .env.example                         <- Environment template
```

---

## Quick Navigation by Use Case

### "I need to understand how the system works"
1. Read: **README_ARCHITECTURE.md** (5-10 min)
2. Read: **CODEBASE_ANALYSIS.md** Sections 1-3 (30 min)
3. Skim: **ARCHITECTURE_QUICK_REFERENCE.md** (10 min)

### "I want to develop a new strategy"
1. Read: **ARCHITECTURE_QUICK_REFERENCE.md** - Strategy Development section
2. Copy: Strategy template from **ARCHITECTURE_QUICK_REFERENCE.md**
3. Reference: **strategies/premarket_momentum_v2.py** (example)
4. Run: `make test` to verify setup
5. Debug: Use logs at `~/grafana-stack/logs/trader.log`

### "I need to fix a bug in the strategy engine"
1. Read: **CODEBASE_ANALYSIS.md** Section 3.1 (Strategy Framework)
2. Review: `/services/strategy/engine.py` and `strategy_base.py`
3. Check: Code review checklist in **README_ARCHITECTURE.md**
4. Run: `pytest tests -k strategy` to test
5. Reference: **ARCHITECTURE_QUICK_REFERENCE.md** - Critical Patterns

### "I need to query the database"
1. Quick reference: **ARCHITECTURE_QUICK_REFERENCE.md** - Database section
2. Full details: **CODEBASE_ANALYSIS.md** Section 3.4 (Database Interactions)
3. Code examples: `/libs/db/clickhouse.py` and `/libs/db/postgres.py`

### "I'm deploying to production"
1. Read: **README_ARCHITECTURE.md** - Deployment Checklist
2. Reference: **ARCHITECTURE_QUICK_REFERENCE.md** - Operational Commands
3. Verify: All environment variables in `.env` file
4. Test: `make test && make verify`
5. Backup: `make backup-pg && make backup-ch`

### "Services won't start"
1. Check: **ARCHITECTURE_QUICK_REFERENCE.md** - Troubleshooting
2. Run: `make verify` to health check
3. Read: **README_ARCHITECTURE.md** - Getting Help section
4. Review: `make logs` for error messages

### "I need to understand the technology stack"
1. Read: **CODEBASE_ANALYSIS.md** Section 2 (Technology Stack)
2. Key files to know: **README_ARCHITECTURE.md** - Key Files to Know
3. Config reference: **ARCHITECTURE_QUICK_REFERENCE.md** - Configuration

---

## Key Concepts Quick Reference

### Architecture Layers
```
Strategy (user code) → Engine (lifecycle) → Decision (validation) 
→ Execution (orders) → Data (DB) → Broker (IBKR)
```

### Data Flow
```
Market Data (IBKR) → Kafka → ClickHouse (bars/features)
                  ↓
             Strategy Logic → Trading Intent (DecisionEnvelope)
                  ↓
             Decision Pipeline (validate, approve)
                  ↓
             Order Execution (IBKR submission) → PostgreSQL (audit)
```

### Key Technologies
- **Language**: Python 3.13
- **Async**: asyncio (built-in)
- **OLAP DB**: ClickHouse
- **OLTP DB**: PostgreSQL (asyncpg)
- **Broker**: Interactive Brokers (ib-insync)
- **Logging**: structlog (JSON to Promtail)
- **Monitoring**: Prometheus + Grafana

### Configuration Priority
1. `.env` file (highest - git-ignored)
2. Environment variables
3. `config.yml`
4. Code defaults (lowest)

---

## Documentation Statistics

| Document | Size | Focus | Best For |
|----------|------|-------|----------|
| README_ARCHITECTURE.md | 14 KB | Overview | Getting started |
| CODEBASE_ANALYSIS.md | 26 KB | Complete | Reference, training |
| ARCHITECTURE_QUICK_REFERENCE.md | 10 KB | Examples | Copy-paste, ops |
| AGENTS.md | 2 KB | Guidelines | Code standards |

**Total: 52 KB of documentation**

---

## Reading Recommendations by Role

### New Developer
1. README_ARCHITECTURE.md (full read)
2. ARCHITECTURE_QUICK_REFERENCE.md (full read)
3. AGENTS.md (full read)
4. CODEBASE_ANALYSIS.md (reference as needed)

### DevOps/Operations
1. ARCHITECTURE_QUICK_REFERENCE.md - Operational Commands
2. README_ARCHITECTURE.md - Deployment Checklist
3. CODEBASE_ANALYSIS.md - Section 8 (Deployment)

### Strategy Developer
1. ARCHITECTURE_QUICK_REFERENCE.md - Strategy Development
2. CODEBASE_ANALYSIS.md - Section 3.1 (Strategy Framework)
3. strategies/premarket_momentum_v2.py (code example)

### Architect/Tech Lead
1. CODEBASE_ANALYSIS.md (full read)
2. README_ARCHITECTURE.md (sections 7-9)
3. ARCHITECTURE_QUICK_REFERENCE.md (reference)

---

## Document Maintenance

**Last Updated**: October 17, 2025
**Generated**: Comprehensive analysis of the trading system codebase
**Accuracy**: Based on actual codebase inspection on October 17, 2025

**To Update**: Follow the patterns and structure established in existing documents

---

## Questions or Issues?

Refer to:
- **Troubleshooting**: ARCHITECTURE_QUICK_REFERENCE.md - Troubleshooting section
- **Best Practices**: CODEBASE_ANALYSIS.md - Section 6 (Common Patterns)
- **Code Review**: README_ARCHITECTURE.md - Code Review Checklist
- **Development**: AGENTS.md - Development Guidelines

