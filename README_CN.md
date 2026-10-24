# Trading Platform v1: 自主高频与多策略量化交易系统

[English](README.md) | [简体中文](README_CN.md)

> **基线开源声明（12个月阶段性成果）**  
> 本代码库为历时 12 个月持续研发的基础架构基线开源版本。商用版本与深度生产环境迭代保留于独立私有仓库维护。本项目依据 MIT 协议开源。

一个集成了 **Apache Kafka 分布式事件流**、**Confluent Schema Registry 数据契约治理**、**双总线混合队列 (MQ)**、**五阶段低延迟流水线**、**AI Brain / LLM 认知大脑**、**深度强化学习 (RL)** 与 **盈透证券 (Interactive Brokers) 直连** 的全自主企业级量化交易操作系统。

---

## ✨ 核心特性

### 1. 生产级事件流骨干与双总线混合架构 (Kafka & Hybrid Event Bus)
- **Apache Kafka 分布式高吞吐集群**：
  - 生产环境采用 3-Broker HA 拓扑，支持 LZ4 硬件级压缩与最小同步副本（`min.insync.replicas=2`）。
  - 作为全系统的持久化事件骨干网（Event Backbone），承担全系统逐笔行情（Tick Trades / Quotes）、微秒级 K 线快照、订单生成、风控拦截到成交确认的全链路异步解耦与削峰填谷。
- **Confluent Schema Registry 数据契约与治理系统**：
  - 基于 Avro / JSON Schema 对核心管道主题进行严格的模式校验与版本演进管理。
  - 强制启用 `BACKWARD` 向后兼容性策略，每条消息均包含版本标识 (`schema_v`)、纳秒级时序戳 (`ts_ns` / `ts_us`)、来源标识 (`producer`) 与全局唯一幂等键 (`idempotency_key`)，彻底杜绝数据漂移与脏数据注入交易执行流。
- **极低延迟双总线架构 (Dual-Bus Architecture)**：
  - **内存状态总线 (Redis Sentinel / Pub-Sub)**：亚毫秒级实时价格广播、最新盘口状态快照、前端仪表盘实时推送与高频状态共享。
  - **持久化事件总线 (Apache Kafka + DLQ)**：严格保序持久化、多消费者组（Consumer Groups）微批入湖、具备指数退避（Exponential Backoff: 1s -> 5s -> 15s）重试机制与死信队列（Dead Letter Queue, DLQ）兜底。

### 2. 五阶段高并发低延迟流水线 (Multi-Phase Reactive Pipeline)
- **Phase 1 (行情接入与入湖)**：直连 IB Gateway，纳秒级解析逐笔 Tick，通过 Lake Writers 微批（Batch 500 / 1.0s）写入 ClickHouse 时序湖，同时推入 Kafka `md.ticks.v1` 与 Redis 内存缓存。
- **Phase 2 (特征引擎与状态计算)**：实时消费行情流，增量计算 VWAP、动态 ATR、多周期动量、订单流不平衡 (Order Flow Imbalance) 与微观结构特征。
- **Phase 3 (信号编排与订单意图)**：多策略并发编排（指数 ETF 动量、均值回归、波动率突破、0DTE/1DTE 期权策略），生成标准 Avro `order.intent.v1` 意图事件推入总线。
- **Phase 4 (订单执行与智能拆单 OMS)**：接收风控通过的 `risk.decision.v2`，执行 TWAP/VWAP/POV 算法拆单与主动撤挂单管理，发布 `exec.order.status.v2` 与成交回报 `exec.fill.v2`。
- **Phase 5 (风控内核与对账闭环)**：前置交易所合规闸（LULD 价格带、SSR 卖空限制、波动率熔断）、幽灵单清理、隔夜强平与资金实时对账，广播 `internal.control` 与健康心跳。

### 3. AI Brain & LLM Trader (认知决策层)
- **AI Brain**：多模型共识裁决引擎，结合宏观经济日历与微观波动率动态调整策略组合的风险暴露。
- **LLM Trader**：基于实时资讯、财报与研究报告的非结构化语义推理，提供独立的情绪加权 Alpha。
- **RL Core**：基于深度强化学习的最优执行算法，最小化大单市场冲击成本与执行滑点。

### 4. 前沿时间序列与机器学习引擎 (ML & HPO)
- 集成最新时间序列深度架构：**iTransformer**、**ETSformer**、**LSTM** 与时序 Transformer 基准。
- 集成梯度提升树（**XGBoost** / **LightGBM** / **CatBoost**）特征重要性评估与自动过滤。
- Walk-Forward 滚动时序交叉验证与 **Optuna** 贝叶斯超参数自动演化框架。

### 5. 全栈生产级可观测性与监控看板 (Observability)
- **Prometheus** 实时采集 Kafka 事件发布速率、消费者组延迟 (Consumer Lag)、DLQ 堆积量与端到端耗时。
- **Grafana** 内置专属 `Event Bus Monitor` 大屏与策略量化损益驾驶舱。
- **ClickHouse** 纳秒级全链路时序日志持久化与 **Streamlit** 实时操盘控制中心。

---

## 🏗 系统架构图

```mermaid
flowchart TD
    subgraph MarketGateway[ 盈透证券行情与网关接入 ]
      IB[Interactive Brokers Gateway]
      MD[Market Data Streamer]
    end

    subgraph MessagingBackbone[ 消息总线与数据契约骨干 (MQ & Event Bus) ]
      SR[Confluent Schema Registry<br/>Avro 严格模式校验 & BACKWARD 兼容]
      KF[Apache Kafka HA Cluster<br/>3-Broker 集群 / 消息持久化 / LZ4 压缩]
      DLQ[Dead Letter Queue<br/>*.dlq 死信兜底]
      RS[Redis Sentinel / Pub-Sub<br/>亚毫秒级内存状态总线]
    end

    subgraph Pipeline[ 五阶段解耦流水线 (Reactive Pipeline) ]
      P1[Phase 1: 行情摄取与校验<br/>md.ticks.v1 / md.tick.trade.v1]
      P2[Phase 2: 实时特征引擎<br/>events.bars / Microstructure]
      P3[Phase 3: 策略信号与意图<br/>order.intent.v1 / internal.plans]
      P4[Phase 4: 订单管理 OMS<br/>exec.order.status.v2 / exec.fill.v2]
      P5[Phase 5: 风控内核与对账<br/>risk.decision.v2 / internal.control]
    end

    subgraph Intelligence[ 智能与决策大脑 (AI Brain) ]
      AIB[AI Brain 仲裁引擎]
      LLM[LLM Trader 语义分析]
      ML[深度时间序列模型 iTransformer/ETSformer]
      RL[RL 强化学习执行优化]
    end

    subgraph Storage[ 时序湖与持久化存储 ]
      LW[Lake Writers 微批入湖]
      CH[(ClickHouse 时序数据湖)]
      PG[(PostgreSQL 关系账本)]
    end

    subgraph Observability[ 全链路观测与监控看板 ]
      PROM[Prometheus Exporter]
      GRAF[Grafana Event Bus Monitor]
      STR[Streamlit 操盘控制台]
    end

    %% Market Data Flow
    IB --> MD
    MD --> P1
    P1 -->|注册模式校验| SR
    P1 -->|逐笔行情流| KF
    P1 -->|实时盘口分发| RS
    P1 -->|微批写入| LW
    LW --> CH

    %% Feature & Strategy Flow
    KF -->|消费行情流| P2
    P2 -->|实时特征| ML
    P2 -->|聚合 K 线| P3
    AIB -->|宏观仲裁| P3
    LLM -->|情绪加权| P3
    ML -->|Alpha 预测| P3
    P3 -->|生成订单意向| KF

    %% Risk & Execution Flow
    KF -->|order.intent.v1| P5
    P5 -->|前置合规放行 risk.decision.v2| KF
    KF -->|消费裁决指令| P4
    P4 -->|算法拆单优化| RL
    RL -->|报单执行| IB
    IB -->|成交回报回灌| P4
    P4 -->|exec.fill.v2| KF
    KF -->|对账与持久化| PG

    %% Fault Tolerance & Telemetry
    KF -.->|重试耗尽失败| DLQ
    KF -.->|生产/消费指标| PROM
    P4 -.->|执行延迟指标| PROM
    P5 -.->|熔断风控指标| PROM
    PROM --> GRAF
    RS --> STR
```

---

## 📊 事件总线主题与数据契约规范 (Event Bus & Schema Registry)

系统通过 `libs/event_bus.py` 实现统一的发布/订阅接口，在 `config.yml` 中声明配置，由 **Confluent Schema Registry** 强制执行 Avro 契约治理：

| 核心主题 (Kafka Topic) | 契约模式 (Schema Registry) | 分区键 (Key) | 生产者 (Producer) | 消费者 (Consumer) | 业务作用与说明 |
|:---|:---|:---|:---|:---|:---|
| `md.ticks.v1` | `TickV1` (Avro) | `symbol` | Phase 1 网关 | SFS 服务、Lake Writers | 逐笔成交与盘口报价合一流，含源端纳秒戳 |
| `events.bars` | `BarV1` (Avro) | `symbol` | Phase 1 / 聚合器 | Phase 2 特征引擎、策略 | 5s / 10s / 1m 多周期标准聚合 K 线 |
| `order.intent.v1` | `OrderIntentV1` (Avro) | `decision_id` | Phase 3 策略集群 | Phase 5 前置风控闸 | 原始下单意图（含策略版本、参数哈希、时效属性） |
| `risk.decision.v2` | `RiskDecisionV2` (Avro) | `decision_id` | Phase 5 风控内核 | Phase 4 订单管理 OMS | 风控裁决结果（批准、驳回、缩量、附加价格保护带） |
| `exec.order.status.v2` | `OrderStatusV2` (Avro) | `order_id` | Phase 4 OMS 网关 | 对账服务、持仓管理器 | 订单生命周期流转（Submitted, PreSubmitted, Cancelled） |
| `exec.fill.v2` | `FillV2` (Avro) | `symbol` | Phase 4 成交监听 | 账本服务、仓位更新 | 逐笔真实成交回报（成交均价、数量、佣金与滑点） |
| `broker.account.v1` | `AccountUpdateV1` (Avro) | `account_id` | IB 网关账单同步 | 资产与杠杆风控器 | 实时购买力、保证金占用与净清算价值快照 |
| `broker.position.v1` | `PositionUpdateV1` (Avro) | `symbol` | IB 持仓同步器 | 幽灵单守护、平仓监视 | 真实底层持仓与浮动盈亏（对齐本地内存账本） |
| `internal.control` | `ControlEventV1` (Avro) | `target` | 风控熔断器 / 运维 | 全系统各阶段服务 | 全局或标的级别紧急暂停、只读模式、恢复交易信号 |
| `*.dlq` | `DLQEnvelopeV1` (JSON) | `original_key` | Event Bus 发布器 | 运维告警、离线补偿 | 3 次重试失败后的死信兜底队列，附带失败现场元数据 |

### 契约示例：`OrderIntentV1` (Avro Schema)
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

### 容灾与死信队列机制 (DLQ & Retry Policy)
- **指数退避重试 (Exponential Backoff)**：默认配置 `[1000ms, 5000ms, 15000ms]`，第 1 次即时重试，第 2 次等待 1 秒，第 3 次等待 5 秒。
- **自动转入死信 (DLQ Fallback)**：当 3 次重试全部耗尽后，发布器自动将原始 Payload、错误原因、调用栈与重试次数打包转入 `<topic>.dlq` 主题，触发 Prometheus `event_bus_publish_total{status="dlq"}` 告警，确保主业务线程永不阻塞或崩溃。

---

## 🏛 高可用基础设施与集群配置 (HA Infrastructure)

项目提供完整的容器化部署配置（支持标准开发模式与三节点高可用集群模式）：

| 服务组件 | 镜像与版本 | 默认端口 | 生产高可用配置 |
|:---|:---|:---|:---|
| **Apache Kafka** | `confluentinc/cp-kafka:7.5.0` | `9092, 9094, 9096` | 3 节点集群，`min.insync.replicas=2`, `replication.factor=3`, LZ4 压缩 |
| **Schema Registry** | `confluentinc/cp-schema-registry:7.5.0` | `8081` | 直连 Kafka 集群，模式向后兼容校验 (`BACKWARD`) |
| **Redis Cache** | `redis:7-alpine` | `6379, 6380, 26379` | 1 Master + 1 Replica + 3 Sentinels 自动故障转移 |
| **ClickHouse** | `clickhouse/clickhouse-server:latest` | `8123, 9000` | 3 节点分布式时序库，ReplicatedMergeTree 副本表 |
| **PostgreSQL** | `postgres:16-alpine` | `5432` | 关系账本存储，支持 TimescaleDB 时序扩展 |
| **Prometheus** | `prom/prometheus:latest` | `9090` | 采集 Event Bus 指标、消费延迟与系统负载 |
| **Grafana** | `grafana/grafana:latest` | `3000` | 预置 `Event Bus Monitor` 与多维度策略面板 |

---

## 📂 模块结构

```text
trading-v1/
├── run_system.py              # 生产主进程入口 (IB Gateway 直连)
├── config.yml                 # 生产级策略、Kafka、风控与流水线配置文件
├── libs/
│   ├── event_bus.py           # 企业级 Kafka 事件总线 (含重试、DLQ与指标)
│   ├── config.py              # 统一配置管理器
│   └── logger.py              # 结构化日志输出
├── ai_brain/                  # AI 认知大脑与动态仲裁决策
├── llm_trader/                # 大语言模型实时辅助决策模块
├── ml/                        # 机器学习特征工程与训练流程
├── rl_core/                   # 强化学习最优执行引擎
├── models/                    # 时间序列深度模型库 (iTransformer/ETSformer/XGB)
├── strategies/                # 交易策略实现 (ETF, 股票, 0DTE/1DTE 期权)
├── services/                  # 核心服务 (行情清洗、OMS、风控、对账、监控)
│   ├── market/                # 行情接入与 1s/5s K线微聚合
│   ├── execution/             # 算法订单执行与成交追踪
│   ├── risk/                  # 前置与后置多层风控拦截内核
│   └── recon/                 # 盈透证券账单与幽灵单对账服务
├── infra/                     # 基础设施支持
│   ├── compose.yml            # 单机极速开发环境 Docker Compose
│   ├── compose.ha.yml         # 生产级 3-Node Kafka + Schema Registry + Redis HA 集群
│   └── init/                  # ClickHouse、Redis Sentinel 与数据库初始化脚本
├── schema/                    # 强类型数据合约 (Avro / Pydantic)
├── dashboards/                # Grafana 监控大屏定义
│   └── active/
│       └── event_bus_monitor.json  # Kafka 事件总线专属监控大屏
├── streamlit_app.py           # Streamlit 交互式操作后台
├── backtest_ml_engine.py      # ML 驱动的历史回测引擎
└── tests/                     # 单元测试与系统集成验证套件
```

---

## 🚀 快速启动

### 1. 启动基础设施集群 (Kafka + Schema Registry + ClickHouse + Redis)
```bash
# 启动标准开发环境
make up

# 或启动 3 节点高可用生产集群
docker compose -f infra/compose.ha.yml up -d
```
启动后验证核心服务：
- **Kafka Bootstrap**: `localhost:9092`
- **Schema Registry UI/API**: `http://localhost:8081`
- **ClickHouse HTTP**: `http://localhost:8123`
- **Grafana 看板**: `http://localhost:3000` (默认账号 admin/admin)

### 2. 检查环境与连通性
```bash
make verify
```

### 3. 配置交易账户与参数
复制示例配置并配置 IB Gateway 端口与账户信息：
```bash
cp config.local.yml.example config.local.yml
```
在 `config.yml` 中调整目标标的（如 `QQQ`, `SPY`, `TQQQ`, `SQQQ`）与风险限额。

### 4. 运行本地回测
```bash
python run_single_backtest.py --strategy qqq_momentum --start 2026-01-01 --end 2026-04-01
```

### 5. 启动实盘/模拟系统
```bash
# 启动完整交易系统服务 (接入 IB Gateway 与 Kafka Event Bus)
python run_system.py

# 启动 Streamlit 可视化监控
streamlit run streamlit_app.py
```

---

## 🔐 三层风控与安全机制

1. **第一层 (交易所合规与硬性熔断闸)**：
   - **LULD 价格带校验**：限价与止损必须落在最新允许价格带内。
   - **SSR 空头限制**：触发 SSR 时严禁以低于或等于当前最佳买价卖空。
   - **标的停牌拦截**：标的处于 Halt / News Pending 状态时硬拒绝新单并撤销未成交单。
2. **第二层 (幽灵单与孤儿挂单防护)**：
   - 实时对齐本地 OMS 状态与 IB 底层账单，毫秒级清理挂单漂移，消除“已挂单但在本地丢失”的幽灵单。
3. **第三层 (隔夜与尾盘强平保护)**：
   - 收盘前 5 分钟针对日内动量与 0DTE/1DTE 期权标的强制平仓，彻底防范隔夜跳空与行权归零风险。

---

## 📄 开源许可证

本项目依据 [MIT 许可证](LICENSE) 分发。  
Copyright (c) 2026 Wei Zhang
