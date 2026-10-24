# Trading Platform v1: Autonomous Algorithmic Quantitative Trading System

[English](README.md) | [简体中文](README_CN.md)

> **Baseline Release Notice (12-Month Open Source)**  
> This repository represents a snapshot of the foundational architecture developed over 12 months of continuous engineering. Production deployments and commercial iterations are maintained in separate private repositories. Distributed under the MIT License.

An enterprise-grade autonomous algorithmic quantitative trading platform featuring an **Apache Kafka event streaming backbone**, **Confluent Schema Registry contract governance**, a **hybrid message bus (MQ)**, a **five-phase low-latency reactive pipeline**, an **AI Brain / LLM Trader cognitive engine**, **deep reinforcement learning (RL) execution**, and direct **Interactive Brokers (IBKR) integration**.

---

## ✨ Core Highlights

### 1. Enterprise Event-Driven Backbone & Hybrid Message Bus (Kafka & MQ)
- **Apache Kafka Distributed High-Throughput Cluster**:
  - High-availability 3-Broker deployment topology with hardware-accelerated LZ4 compression and strict in-sync replicas (`min.insync.replicas=2`).
  - Acts as the immutable event streaming backbone, decoupling market tick trades/quotes, microsecond bar snapshots, order intents, risk decisions, and execution fills for seamless backpressure protection and load smoothing.
- **Confluent Schema Registry Contract Governance**:
  - Enforces strict Avro / JSON Schema contracts across all core pipeline topics.
  - Implements a mandatory `BACKWARD` compatibility policy. Every message incorporates schema versioning (`schema_v`), microsecond/nanosecond timestamps (`ts_us` / `ts_ns`), producer provenance (`producer`), and unique idempotency keys (`idempotency_key`), completely preventing schema drift and dirty data contamination.
- **Dual-Bus Low-Latency Hybrid Architecture**:
  - **In-Memory State Bus (Redis Sentinel / Pub-Sub)**: Sub-millisecond market price broadcasting, live order book cache, and real-time frontend telemetry feeds.
  - **Durable Event Bus (Apache Kafka + DLQ)**: Strictly-ordered durable persistence, multi-consumer group micro-batch ingestion, exponential backoff retries (1s -> 5s -> 15s), and Dead Letter Queue (DLQ) automated fallback.

### 2. Five-Phase Reactive Low-Latency Pipeline
- **Phase 1 (Market Ingestion & Lake Writing)**: Direct IB Gateway connection parsing sub-second ticks, streaming simultaneously to Lake Writers (Batch 500 / 1.0s) into the ClickHouse time-series lake, Kafka `md.ticks.v1`, and Redis cache.
- **Phase 2 (Real-Time Feature Engine)**: Consumes market event streams, calculating incremental VWAP, dynamic ATR, multi-timeframe momentum, Order Flow Imbalance (OFI), and market microstructure metrics.
- **Phase 3 (Signal & Strategy Orchestration)**: Multi-strategy orchestration spanning Index ETF momentum (QQQ, SPY, TQQQ, SQQQ), mean reversion, volatility breakout, and 0DTE/1DTE options, emitting standardized Avro `order.intent.v1` events.
- **Phase 4 (Order Management System - OMS)**: Consumes pre-cleared `risk.decision.v2` events, executing algorithmic order slicing (TWAP, VWAP, POV), active quote pegging, and publishing order status and fill events (`exec.fill.v2`).
- **Phase 5 (Risk Kernel & Reconciliation)**: Enforces pre-trade regulatory gates (LULD price bands, Reg SHO SSR short-sale rules, trading halt interlocks), ghost-order reconciliation, and ledger balance verification, broadcasting `internal.control` and health heartbeats.

### 3. AI Brain & LLM Trader (Cognitive Layer)
- **AI Brain**: Multi-model consensus arbitration engine dynamically tuning risk exposure based on macroeconomic calendar events and market regime shifts.
- **LLM Trader**: Semantic sentiment reasoning on real-time financial news, earnings disclosures, and analyst reports to derive uncorrelated alpha weighting.
- **RL Core**: Deep reinforcement learning execution agents optimized to minimize implementation shortfall and slippage.

### 4. Advanced Machine Learning & Hyperparameter Evolution (ML & HPO)
- State-of-the-art time-series deep learning models: **iTransformer**, **ETSformer**, **LSTM**, and temporal Transformer baselines.
- Gradient boosted decision trees (**XGBoost**, **LightGBM**, **CatBoost**) for non-linear alpha factor ranking and automated pruning.
- Walk-forward rolling validation coupled with **Optuna** Bayesian optimization for continuous parameter tuning.

### 5. Production-Grade Telemetry & Observability
- **Prometheus** real-time metrics scraping event publishing throughput, consumer lag, DLQ rates, and p99 pipeline latencies.
- **Grafana** dedicated `Event Bus Monitor` dashboard alongside strategy P&L performance cockpits.
- **ClickHouse** nanosecond audit logging paired with a **Streamlit** live operations cockpit.

---

## 🏗 Architecture Overview

```mermaid
flowchart TD
    subgraph MarketGateway[ Interactive Brokers Gateway & Market Data ]
      IB[Interactive Brokers Gateway]
      MD[Market Data Streamer]
    end

    subgraph MessagingBackbone[ Message Queue & Event Bus Backbone (MQ & Event Bus) ]
      SR[Confluent Schema Registry<br/>Strict Avro Validation & BACKWARD Compatibility]
      KF[Apache Kafka HA Cluster<br/>3-Broker Cluster / Persistence / LZ4 Compression]
      DLQ[Dead Letter Queue<br/>*.dlq Automated Fallback]
      RS[Redis Sentinel / Pub-Sub<br/>Sub-Millisecond State Bus]
    end

    subgraph Pipeline[ Five-Phase Reactive Pipeline ]
      P1[Phase 1: Market Ingestion<br/>md.ticks.v1 / md.tick.trade.v1]
      P2[Phase 2: Feature Engine<br/>events.bars / Microstructure]
      P3[Phase 3: Strategy Intent<br/>order.intent.v1 / internal.plans]
      P4[Phase 4: Order Management OMS<br/>exec.order.status.v2 / exec.fill.v2]
      P5[Phase 5: Risk Kernel & Recon<br/>risk.decision.v2 / internal.control]
    end

    subgraph Intelligence[ Intelligence & Decision Brain ]
      AIB[AI Brain Consensus Engine]
      LLM[LLM Trader Sentiment Parser]
      ML[Deep Time-Series Models iTransformer/ETSformer]
      RL[RL Execution Optimization]
    end

    subgraph Storage[ Time-Series Lake & Persistence ]
      LW[Lake Writers Micro-Batch]
      CH[(ClickHouse Time-Series Lake)]
      PG[(PostgreSQL Trade Ledger)]
    end

    subgraph Observability[ Telemetry & Observability ]
      PROM[Prometheus Exporter]
      GRAF[Grafana Event Bus Monitor]
      STR[Streamlit Operations Cockpit]
    end

    %% Market Data Flow
    IB --> MD
    MD --> P1
    P1 -->|Register & Validate Schema| SR
    P1 -->|Tick Stream| KF
    P1 -->|Order Book Broadcast| RS
    P1 -->|Micro-Batch Ingest| LW
    LW --> CH

    %% Feature & Strategy Flow
    KF -->|Consume Tick Stream| P2
    P2 -->|Real-Time Features| ML
    P2 -->|Aggregated Bars| P3
    AIB -->|Macro Regime| P3
    LLM -->|Sentiment Alpha| P3
    ML -->|Alpha Predictions| P3
    P3 -->|Publish Order Intent| KF

    %% Risk & Execution Flow
    KF -->|order.intent.v1| P5
    P5 -->|Pre-Trade Clearance risk.decision.v2| KF
    KF -->|Consume Approved Decisions| P4
    P4 -->|Algo Slicing Optimization| RL
    RL -->|Execute Orders| IB
    IB -->|Execution Fills| P4
    P4 -->|exec.fill.v2| KF
    KF -->|Audit Reconciliation| PG

    %% Fault Tolerance & Telemetry
    KF -.->|Retries Exhausted| DLQ
    KF -.->|Bus & Consumer Lag Metrics| PROM
    P4 -.->|Latency Metrics| PROM
    P5 -.->|Circuit Breaker Metrics| PROM
    PROM --> GRAF
    RS --> STR
```

---

## 📊 Event Bus Topics & Schema Registry Specification

The system employs a unified event bus interface via `libs/event_bus.py`, defined in `config.yml`, with Avro contract governance enforced by **Confluent Schema Registry**:

| Kafka Topic | Contract Schema | Keying | Producer | Consumer | Functional Role & SLA |
|:---|:---|:---|:---|:---|:---|
| `md.ticks.v1` | `TickV1` (Avro) | `symbol` | Phase 1 Gateway | SFS, Lake Writers | Unified trades and quotes tick stream with nanosecond source timestamps |
| `events.bars` | `BarV1` (Avro) | `symbol` | Phase 1 / Aggregator | Phase 2 Features, Strategies | 5s / 10s / 1m standardized OHLCV bar aggregations |
| `order.intent.v1` | `OrderIntentV1` (Avro) | `decision_id` | Phase 3 Strategies | Phase 5 Risk Kernel | Raw order intentions with strategy version, parameter hash, and TIF constraints |
| `risk.decision.v2` | `RiskDecisionV2` (Avro) | `decision_id` | Phase 5 Risk Kernel | Phase 4 OMS | Pre-trade risk evaluation verdict (Approved, Rejected, Sized-down, Price Collared) |
| `exec.order.status.v2` | `OrderStatusV2` (Avro) | `order_id` | Phase 4 OMS Gateway | Recon Service, Position Mgr | Order state transitions (Submitted, PreSubmitted, Cancelled, Filled) |
| `exec.fill.v2` | `FillV2` (Avro) | `symbol` | Phase 4 Execution | Ledger Service, Position Mgr | Individual verified execution fills (average price, quantity, commission, slippage) |
| `broker.account.v1` | `AccountUpdateV1` (Avro) | `account_id` | IB Gateway Sync | Portfolio & Margin Risk | Real-time buying power, margin requirements, and net liquidation snapshot |
| `broker.position.v1` | `PositionUpdateV1` (Avro) | `symbol` | IB Position Sync | Ghost Guard, Exit Monitors | Broker ground-truth positions and unrealized PnL (aligns local ledger) |
| `internal.control` | `ControlEventV1` (Avro) | `target` | Circuit Breakers / Ops | All Pipeline Phase Workers | System-wide or symbol-specific pause, read-only mode, and resume commands |
| `*.dlq` | `DLQEnvelopeV1` (JSON) | `original_key` | Event Bus Publisher | Ops Alerts, Offline Replay | Dead Letter Queue capturing payloads after retry exhaustion with error context |

### Contract Sample: `OrderIntentV1` (Avro Schema)
```json
{
  "type": "record",
  "name": "OrderIntentV1",
  "namespace": "trading.events",
  "fields": [
    { "name": "schema_v", "type": "int" },
    {
      "name": "metadata",
      "type": {
        "type": "record",
        "name": "EventMetadata",
        "fields": [
          { "name": "ts_us", "type": "long" },
          { "name": "producer", "type": "string" },
          { "name": "idempotency_key", "type": ["null", "string"], "default": null }
        ]
      }
    },
    {
      "name": "data",
      "type": {
        "type": "record",
        "name": "IntentData",
        "fields": [
          { "name": "decision_id", "type": "string" },
          { "name": "strategy_id", "type": "string" },
          { "name": "symbol", "type": "string" },
          { "name": "side", "type": { "type": "enum", "name": "Side", "symbols": ["BUY", "SELL", "SELL_SHORT"] } },
          { "name": "qty", "type": "double" },
          { "name": "order_type", "type": { "type": "enum", "name": "OrdType", "symbols": ["MKT", "LMT", "STP", "STP_LMT"] } },
          { "name": "limit_px", "type": ["null", "double"], "default": null },
          { "name": "stop_px", "type": ["null", "double"], "default": null },
          { "name": "tif", "type": "string", "default": "DAY" },
          { "name": "allow_prepost", "type": "boolean", "default": false }
        ]
      }
    }
  ]
}
```

### DLQ & Fault Tolerance Policy
- **Exponential Backoff**: Configured backoff delays `[1000ms, 5000ms, 15000ms]`: 1st retry immediate, 2nd retry waits 1s, 3rd retry waits 5s.
- **Dead Letter Queue (DLQ)**: When all 3 attempts fail, the publisher forwards the original payload, key, timestamp, and exception details to `<topic>.dlq`. This increments the Prometheus metric `event_bus_publish_total{status="dlq"}` and avoids crashing active trading loops.

---

## 🏛 High-Availability Infrastructure (HA Topology)

The system supports containerized standalone development or a 3-node HA production cluster:

| Component | Image & Tag | Default Port | Production HA Setup |
|:---|:---|:---|:---|
| **Apache Kafka** | `confluentinc/cp-kafka:7.5.0` | `9092, 9094, 9096` | 3-node cluster, `min.insync.replicas=2`, `replication.factor=3`, LZ4 compression |
| **Schema Registry** | `confluentinc/cp-schema-registry:7.5.0` | `8081` | Direct Kafka bootstrap connection, strict `BACKWARD` contract validation |
| **Redis Cache** | `redis:7-alpine` | `6379, 6380, 26379` | 1 Master + 1 Replica + 3 Sentinels automated failover |
| **ClickHouse** | `clickhouse/clickhouse-server:latest` | `8123, 9000` | 3-node cluster with ReplicatedMergeTree tables |
| **PostgreSQL** | `postgres:16-alpine` | `5432` | Relational trade ledger with TimescaleDB time-series support |
| **Prometheus** | `prom/prometheus:latest` | `9090` | Scrapes Event Bus throughput, consumer lag, and host resources |
| **Grafana** | `grafana/grafana:latest` | `3000` | Preloaded `Event Bus Monitor` and strategy dashboards |

---

## 📂 Repository Layout

```text
trading-v1/
├── run_system.py              # Main production entry point (IB Gateway connection)
├── config.yml                 # Production strategy, Kafka, risk, and OMS configuration
├── libs/
│   ├── event_bus.py           # Enterprise Kafka event bus (retries, DLQ, Prometheus metrics)
│   ├── config.py              # Centralized configuration manager
│   └── logger.py              # Structured JSON/Console logger
├── ai_brain/                  # AI cognitive arbitration and dynamic decision loops
├── llm_trader/                # Real-time LLM market sentiment ingestion
├── ml/                        # Feature engineering, dataset preparation, and training
├── rl_core/                   # Reinforcement learning execution engine
├── models/                    # Time-series deep models (iTransformer, ETSformer, XGB)
├── strategies/                # Trading strategies (ETFs, Equities, 0DTE/1DTE Options)
├── services/                  # Core services (Market data, OMS, Risk, Recon, Health)
│   ├── market/                # Market ingestion and 1s/5s micro-aggregators
│   ├── execution/             # Algorithmic order execution and fill reconciliation
│   ├── risk/                  # Multi-tier pre-trade and post-trade risk kernels
│   └── recon/                 # Broker ledger synchronization and ghost-order guards
├── infra/                     # Infrastructure configurations
│   ├── compose.yml            # Standalone development Docker Compose stack
│   ├── compose.ha.yml         # 3-Node Kafka + Schema Registry + Redis HA cluster
│   └── init/                  # ClickHouse, Redis Sentinel, and PostgreSQL init scripts
├── schema/                    # Strongly-typed data schemas (Avro / Pydantic)
├── dashboards/                # Grafana operational dashboards
│   └── active/
│       └── event_bus_monitor.json  # Dedicated Event Bus monitoring dashboard
├── streamlit_app.py           # Streamlit live operational cockpit
├── backtest_ml_engine.py      # ML-driven historical backtesting engine
└── tests/                     # Unit and integration test suite
```

---

## 🚀 Quick Start

### 1. Launch Infrastructure (Kafka + Schema Registry + ClickHouse + Redis)
```bash
# Launch standard standalone environment
make up

# Or launch 3-node HA production cluster
docker compose -f infra/compose.ha.yml up -d
```
Verify core services:
- **Kafka Bootstrap**: `localhost:9092`
- **Schema Registry UI/API**: `http://localhost:8081`
- **ClickHouse HTTP**: `http://localhost:8123`
- **Grafana Dashboard**: `http://localhost:3000` (default credentials: `admin` / `admin`)

### 2. Verify Infrastructure Health
```bash
make verify
```

### 3. Configure Trading Account
Copy the local template and populate your IBKR Gateway port and paper/live account credentials:
```bash
cp config.local.yml.example config.local.yml
```
Adjust symbol universes (e.g., `QQQ`, `SPY`, `TQQQ`, `SQQQ`) and risk thresholds in `config.yml`.

### 4. Run a Backtest
```bash
python run_single_backtest.py --strategy qqq_momentum --start 2026-01-01 --end 2026-04-01
```

### 5. Launch Live / Paper Trading System
```bash
# Launch trading engine connected to IB Gateway and Kafka Event Bus
python run_system.py

# Launch Streamlit monitoring cockpit
streamlit run streamlit_app.py
```

---

## 🔐 Three-Tier Risk Control Architecture

1. **Tier 1 (Regulatory Hard Gates & Circuit Breakers)**:
   - **LULD Band Validation**: Limit and stop prices must fall strictly within the prevailing Limit-Up / Limit-Down price band.
   - **Reg SHO Short-Sale Restrictions (SSR)**: Prohibits selling short at or below the National Best Bid when SSR is triggered.
   - **Trading Halt Interlocks**: Immediate rejection of orders for halted symbols and automated cancellation of open leaves.
2. **Tier 2 (Ghost Order & Ledger Reconciliation)**:
   - Real-time synchronization between OMS pending states and broker execution receipts to immediately terminate orphaned orders.
3. **Tier 3 (End-of-Day Liquidation)**:
   - Automated pre-close flattening (5 minutes before 16:00 ET) for intraday strategies and 0DTE/1DTE options to prevent overnight gap risk.

---

## 📄 License

Distributed under the [MIT License](LICENSE).  
Copyright (c) 2026 Wei Zhang
