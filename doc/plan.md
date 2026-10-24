
### **第一部分：开发前阅读顺序（建立系统全局观）**

在开始编码之前，强烈建议按以下顺序通读设计文档，以建立从宏观到微观的完整认知。.env文件里面配置了所有可能用到的接口，postgres sql连接方式，ibkr api gateway 连接等信息。

1.  **全局框架与核心理念 (Big Picture)**
    * **`doc/2.md`**: **必读起点**。定义了重写的目标、高层架构（IoC）、组件职责和 SLO。
    * **`doc/3.md`**: 确立了关键技术选型：ClickHouse、250ms tick 级数据处理、5s 策略调度和离线预计算。
    * **`doc/1.md`**: 提供了对旧系统的快照分析，有助于理解重写的背景和需要保留/改进的关键点。

2.  **安全内核与执行生命周期 (Safety Core & Execution)**
    * **`doc/4.md`, `doc/5.md`, `doc/6.md`**: 强调“安全内核优先”原则，并给出了 DDL、原子事务流程和状态机的初步规范。
    * **`doc/17.md`**: 提供了 PostgreSQL 风控账本和资金预留的**完整 DDL 与存储过程**。
    * **`doc/18.md` & `doc/30.md`**: 详细定义了**执行安全内核**，特别是 OCO、追踪止损和改价节流状态机。
    * **`doc/31.md`, `doc/53.md`, `doc/54.md`**: 深入到执行细节，包括**强一致撤改(CRS)**、**资金预留泄漏看门狗**和**OCO/反手编舞**。
    * **`doc/32.md`**: 介绍了可插拔的**追踪止损策略模块**（ATR/通道/斜率）。

3.  **数据平面：从源头到消费 (Data Plane)**
    * **`doc/11.md`**: 定义了从 IBKR 到 Kafka 的**数据桥和 Bar 聚合**规范。
    * **`doc/25.md` - `doc/29.md`**: 提供了 250ms tick 聚合器、ClickHouse Writer、回补服务和Prometheus 指标的**完整可运行骨架**。
    * **`doc/10.md`, `doc/33.md`, `doc/57.md`**: 定义了引擎如何通过 **DAL (数据访问层)** 从 ClickHouse 读取数据。
    * **`doc/58.md`**: 规范了**特征层**（features_5s/10s）的计算与落地。
    * `doc/clickhouse.md`: 提供了 ClickHouse 的安装与连接基础信息。

4.  **策略与决策引擎 (Strategy & Decision Engine)**
    * **`doc/8.md`, `doc/9.md`**: 定义了策略开发的**抽象接口（`StrategyAlgorithm`）**和引擎骨架。
    * **`doc/20.md` - `doc/24.md`**: 提供了**可运行的策略引擎骨架**，包括 Kafka 和 ClickHouse 的适配器。
    * **`doc/49.md` - `doc/52.md` & `doc/55.md`, `doc/56.md`**: 详细阐述了从策略信号到执行意图的完整“决策管线”，包括**决策信封(Envelope)、校验器(Validator)、翻译器(Translator)、仲裁器(Arbiter)和新鲜度/TTL**。
    * **`doc/59.md`, `doc/65.md`, `doc/67.md`**: 定义了**策略 SDK、规则 DSL 引擎和模板库**，以及如何从旧策略迁移。

5.  **生产就绪与运维 (Production Readiness)**
    * **`doc/12.md` - `doc/16.md`**: 提供了**执行路线图、任务清单和 E2E 测试Harness**，是项目管理的直接输入。
    * **`doc/34.md` - `doc/39.md` & `doc/41.md` - `doc/44.md`, `doc/46.md` - `doc/48.md`, `doc/51.md`, `doc/64.md`, `doc/68.md` - `doc/78.md`**: 这是一个庞大的 P-Phase 系列文档，覆盖了从**投资组合服务(Portfolio)、风险服务(Risk)、IB 适配器、监管规则(Reg-Lite)、错误码矩阵、共享限流、日历、冷启动对账 2.0，到动态资金配额、账户保证金实时账本和统一回测**等所有生产级组件。
    * **`doc/40.md`**: Grafana Dashboard JSON，可直接导入。
    * **`doc/45.md`**: **事件契约(Avro Schemas)**，数据流的“法律”。
    * **`doc/60.md` - `doc/63.md`, `doc/66.md`, `doc/70.md`**: 定义了完整的**发布流水线、GameDay 演练、T+0/T+1 报表和 Exactly-Once 接线**。

---

### **第二部分：给 Claude Code 的分阶段落地计划（完整版）**

这个顺序确保了系统的**安全底座**最先建立，然后**纵向打通**数据流和执行流，最后进行生产硬化和效率提升。

#### **阶段 0：基础设施与核心契约 (Infrastructure & Core Contracts)**

* **任务**:
    1.  **数据库与消息队列**: 搭建 PostgreSQL (`doc/5.md`, `doc/17.md`, `doc/42.md`, `doc/75.md`), ClickHouse (`doc/3.md`, `doc/57.md`, `doc/63.md`), Kafka (`doc/7.md`), Redis。
    2.  **事件契约**: 将 `doc/45.md`, `doc/57.md` 的 Avro Schemas 注册到 Schema Registry。
    3.  **监控**: 部署 Prometheus/Grafana, 导入 `doc/40.md` 的 Dashboard JSON。
* **关键文档**: `doc/5.md`, `doc/17.md`, `doc/45.md`, `doc/40.md`, `doc/clickhouse.md`。

#### **阶段 1：安全内核与执行闭环 (Safety Core & Execution Loop)**

* **任务**:
    1.  **IB 适配与事件桥**: 实现 `IBInsyncAdapter` (`doc/36.md`), `IBEventBridge` (`doc/36.md`), 和 `IBAccountBridge` (`doc/37.md`)。
    2.  **执行安全内核**: 实现 `SafetyKernel` (`doc/30.md`), `CRS` (`doc/31.md`), OCO/反手编舞 (`doc/54.md`)。
    3.  **资金预留与风控**: 实现 `ReservationWatchdog` (`doc/53.md`), `RiskService` 最小版 (`doc/35.md`)，并接入 Reg-Lite (`doc/44.md`, `doc/51.md`) 和 Error Matrix (`doc/52.md`)。
    4.  **冷启动与健康检查**: 实现 `Reconciler 2.0` (`doc/39.md`) 和全局 Kill-Switch (`doc/38.md`)。
* **关键文档**: `doc/17.md`, `doc/18.md`, `doc/30.md`, `doc/31.md`, `doc/35.md`, `doc/36.md`, `doc/37.md`, `doc/39.md`, `doc/44.md`, `doc/51.md`, `doc/52.md`, `doc/53.md`, `doc/54.md`。

#### **阶段 2：数据平面贯通 (Data Plane Integration)**

* **任务**:
    1.  **行情采集与聚合**: 实现 `FiveSecondAggregator` (`doc/25.md`, `doc/28.md`, `doc/72.md`)。
    2.  **数据落地与回补**: 实现 `CHWriter` (`doc/26.md`) 和 `BackfillService` (`doc/29.md`)。
    3.  **特征工程**: 部署 `doc/58.md` 的 SQL, 实现特征增量计算与发布。
    4.  **数据访问层**: 实现 `MarketDataFacade` (`doc/10.md`, `doc/33.md`) 和 `MarketStateProvider` (`doc/46.md`, `doc/47.md`, `doc/48.md`)。
* **关键文档**: `doc/10.md`, `doc/11.md`, `doc/25.md` - `doc/29.md`, `doc/33.md`, `doc/46.md`, `doc/47.md`, `doc/48.md`, `doc/57.md`, `doc/58.md`, `doc/72.md`。

#### **阶段 3：策略决策全链路 (Strategy & Decision Pipeline)**

* **任务**:
    1.  **策略引擎与SDK**: 实现 `StrategyEngine` (`doc/23.md`, `doc/24.md`) 和 `StrategyAlgorithm` API (`doc/8.md`, `doc/59.md`)。
    2.  **决策管线**: 依次实现 **Validator** (`doc/50.md`), **Arbiter** (`doc/55.md`), **Translator** (`doc/50.md`)，并集成 **Freshness/TTL** (`doc/56.md`, `doc/61.md`)。
    3.  **动态资金**: 实现 **动态配额** (`doc/64.md`, `doc/77.md`) 和 **P4 账户保证金账本** (`doc/68.md`, `doc/69.md`)。
    4.  **权威账本**: 实现 `PortfolioService` (`doc/75.md`) 和**逐笔 PnL 作业** (`doc/63.md`, `doc/66.md`, `doc/70.md`)。
    5.  **规则引擎与迁移**: 实现 **Rule DSL 引擎** (`doc/65.md`) 和旧策略适配层 (`doc/67.md`)。
* **关键文档**: `doc/8.md`, `doc/20.md`-`doc/24.md`, `doc/49.md`-`doc/52.md`, `doc/55.md`, `doc/56.md`, `doc/59.md`, `doc/61.md`, `doc/63.md`-`doc/65.md`, `doc/67.md`-`doc/71.md`, `doc/75.md`, `doc/77.md`。

#### **阶段 4：生产硬化与自动化 (Production Hardening & Automation)**

* **任务**:
    1.  **发布与回滚**: 实现**发布流水线** (`doc/60.md`) 和**灰度对齐脚本** (`doc/62.md`)。
    2.  **容灾与HA**: 部署 P8 (`doc/76.md`) 定义的**检查点、回放与健康契约**，并执行 **Runbook 演练** (`doc/66.md`)。
    3.  **统一回测**: 实现 `SimAdapter` 和**回测/影子一致性**框架 (`doc/78.md`, `doc/73.md`)。
    4.  **审计与合规**: 固化**EOD 对账** (`doc/42.md`) 和**全链路审计** (`doc/41.md`, `doc/43.md`)。
* **关键文档**: `doc/41.md`, `doc/42.md`, `doc/43.md`, `doc/60.md`, `doc/62.md`, `doc/66.md`, `doc/73.md`, `doc/76.md`, `doc/78.md`。

这个详尽的计划确保了每一份设计文档都在其最合适的位置被实现，构建了一个健壮、安全且高效的交易系统。