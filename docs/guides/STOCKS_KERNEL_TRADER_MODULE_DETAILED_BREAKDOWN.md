# Stocks Kernel Trader Module Detailed Breakdown

目的：把 [STOCKS_KERNEL_TRADER_MODULE_TODO_TEST_ACCEPTANCE.md](./docs/guides/STOCKS_KERNEL_TRADER_MODULE_TODO_TEST_ACCEPTANCE.md) 里的“大框架”继续拆成可开发、可分工、可测试、可验收的“小框架”。  
原则：每个大模块都要继续拆成：

1. 子模块
2. 标准 contract
3. 输入输出
4. 依赖关系
5. 开发 Todo
6. 测试点
7. 验收点

## 0. 实施原则

1. 先 contract，后实现。
2. 先状态机，后策略花样。
3. 先保护资金，后优化收益。
4. 先平台通用性，后单策略 parity。
5. 每个模块必须能独立测试，不能只靠整链路冒烟。

## 1. 数据域详细拆解

### 1.1 实时行情数据模块

#### 子模块

1. `TradeSnapshotProvider`
2. `QuoteSnapshotProvider`
3. `ExecSymbolSnapshotProvider`
4. `SignalSymbolSnapshotProvider`
5. `SnapshotFreshnessEvaluator`
6. `SnapshotFallbackPolicy`

#### 标准 Contract

1. `last_trade_ts`
2. `bid_ts`
3. `ask_ts`
4. `last_trade_px`
5. `bid_px`
6. `ask_px`
7. `session`
8. `snapshot_source`
9. `freshness_ok`
10. `quote_ready`
11. `trade_ready`
12. `exec_ready`

#### 开发 Todo

1. 区分 `signal snapshot` 和 `exec snapshot`
2. 明确 quote 不全时哪些 action 可以继续
3. 明确不同 session 的 fallback 语义
4. 建立 snapshot 缺失审计标准

#### 测试点

1. `signal ready / exec stale`
2. `signal stale / exec ready`
3. `bid/ask missing`
4. `trade stale but quote fresh`
5. `PRE/POST/OVERNIGHT` 差异

#### 验收点

1. 每个 action 都有明确最低 snapshot 要求
2. snapshot 不足时行为可解释
3. 不会在脏 snapshot 上继续 entry

### 1.2 历史行情数据模块

#### 子模块

1. `HistoryLoader`
2. `HistoryContinuityChecker`
3. `SessionBoundaryNormalizer`
4. `WarmupWindowBuilder`
5. `HistoryFallbackPolicy`

#### 标准 Contract

1. `min_bars_required`
2. `bars_loaded`
3. `continuity_ok`
4. `gap_count`
5. `largest_gap_sec`
6. `session_boundary_ok`
7. `warmup_ready`

#### 开发 Todo

1. 定义 warmup 与 trading-ready 的区别
2. 明确跨 session 历史拼接规则
3. 明确历史缺口时是 block 还是 degrade
4. 历史数据 contract 从策略层抽到 kernel

#### 测试点

1. bars 不足
2. bars 足够但 continuity 断裂
3. 开盘前 warmup 完整
4. 重启后 history rebuild

#### 验收点

1. warmup 失败不会进入正常 entry
2. continuity 风险有审计、有门控

### 1.3 实时特征数据模块

#### 子模块

1. `FeatureComputer`
2. `FeatureFreshnessEvaluator`
3. `FeatureCriticalityClassifier`
4. `FeatureDegradePolicy`

#### 标准 Contract

1. `feature_id`
2. `value`
3. `ready`
4. `reason`
5. `criticality`
6. `max_age_sec`
7. `degraded`

#### 开发 Todo

1. 区分 entry-critical / exit-critical / optional feature
2. feature readiness 进入 action-level 语义
3. evaluator 输出 degraded feature 列表

#### 测试点

1. entry-critical feature 缺失
2. exit-critical feature 缺失
3. optional feature 缺失但允许 degrade

#### 验收点

1. 缺 feature 时不会静默交易
2. 审计能说明是哪类 feature 阻断了 action

### 1.4 历史特征数据模块

#### 子模块

1. `SeedStateBuilder`
2. `AnchorStateBuilder`
3. `OpeningWindowFeatureBuilder`
4. `HistoricalFeatureReplay`

#### 标准 Contract

1. `seed_ready`
2. `seed_sample_count`
3. `anchor_source`
4. `opening_window_ready`
5. `historical_feature_version`

#### 开发 Todo

1. 把 seed / anchor 抽成标准 feature 模块
2. 做历史特征 replay parity
3. 定义历史特征持久化边界

#### 测试点

1. restart 前后历史特征一致
2. session 切换后特征不漂移

#### 验收点

1. 交易员可以声明“这个策略依赖哪类历史特征”

## 2. 市场状态与标的域详细拆解

### 2.1 市场状态 / Session / Regime 模块

#### 子模块

1. `SessionStateResolver`
2. `RegimeClassifier`
3. `NoNewEntryWindow`
4. `FlattenWindow`
5. `HighVolRegimeFlag`
6. `LowLiquidityRegimeFlag`

#### 标准 Contract

1. `session`
2. `regime`
3. `new_entry_allowed`
4. `flatten_due`
5. `degrade_level`

#### 开发 Todo

1. 建立统一 session-regime 输出
2. 将时间窗口从策略判断下沉为平台模块
3. 建立 regime -> gate / action 影响规则

#### 测试点

1. `PRE -> RTH -> POST -> OVERNIGHT`
2. flatten-before-close
3. no-new-after
4. regime 切换触发 action 变化

#### 验收点

1. 交易员无需手写时间判断
2. session/regime 对行为影响清晰可解释

### 2.2 标的定义 / Universe 模块

#### 子模块

1. `InstrumentDefinition`
2. `UniverseDefinition`
3. `LaneDefinition`
4. `ExecutionPreference`
5. `ProtectionLaneDefinition`
6. `HedgeLaneDefinition`

#### 标准 Contract

1. `signal_symbol`
2. `execution_lanes`
3. `protection_lanes`
4. `hedge_lanes`
5. `allowed_sessions`
6. `exchange_preference`
7. `outside_rth_policy`

#### 开发 Todo

1. 将当前 instrument binding 扩成完整 definition
2. 支持单标的、双腿、同标的执行
3. 增加 universe 级过滤与优先级

#### 测试点

1. 单 signal -> 单 exec
2. 单 signal -> 双 exec
3. 保护腿独立
4. 不同 session 下 execution preference 生效

#### 验收点

1. 交易员可以只通过配置定义标的与执行腿

## 3. 策略表达域详细拆解

### 3.1 信号逻辑模块

#### 子模块

1. `DirectionalSignal`
2. `StrengthSignal`
3. `EntryTriggerSignal`
4. `AddSignal`
5. `ReduceSignal`
6. `ExitSignal`
7. `ReverseSignal`

#### 标准 Contract

1. `signal_id`
2. `direction`
3. `strength`
4. `confidence`
5. `action_candidates`
6. `signal_reason`

#### 开发 Todo

1. action candidate 规范化
2. 信号与执行语义分离
3. 支持多 action 排序，而非单 entry

#### 测试点

1. 单一 enter signal
2. 多 action candidate 同时输出
3. signal 与 gate 解耦正确

#### 验收点

1. 交易员可表达“信号想做什么”
2. kernel 决定“最终执行什么”

### 3.2 门控逻辑模块

#### 子模块

1. `DataIntegrityGate`
2. `SeedReadinessGate`
3. `StartupRecoveryGate`
4. `OpenOrderGate`
5. `RealPositionInsuranceGate`
6. `SessionGate`
7. `CooldownGate`
8. `PortfolioGate`

#### 标准 Contract

1. `gate_id`
2. `allowed`
3. `severity`
4. `recoverable`
5. `block_scope`
6. `retry_after_sec`
7. `payload`

#### 开发 Todo

1. 丰富 gate 输出语义
2. gate 支持 block 某一 action 而不是 block 全策略
3. gate 支持 fail-open/fail-closed

#### 测试点

1. 单 gate block
2. 多 gate 同时 block
3. recoverable gate 自动恢复

#### 验收点

1. 交易员能解释“为什么这笔单没发出去”

### 3.3 仓位 Sizing 模块

#### 子模块

1. `FixedDollarSizer`
2. `FixedSharesSizer`
3. `ATRRiskSizer`
4. `VolatilityAdjustedSizer`
5. `ConvictionSizer`
6. `AddOnSizer`

#### 标准 Contract

1. `sizing_mode`
2. `planned_qty`
3. `planned_notional`
4. `risk_unit`
5. `capital_limit_usd`
6. `reservation_hint`

#### 开发 Todo

1. sizing 模块独立化
2. sizing 与 risk reservation 一致化
3. sizing 与 protection qty 一致化

#### 测试点

1. fixed dollar
2. ATR risk
3. volatility adjusted
4. add-on sizing

#### 验收点

1. 交易员只改 sizing 配置就能换仓位模型

## 4. 执行域详细拆解

### 4.0 动作仲裁 / 优先级裁决模块

#### 子模块

1. `ActionArbiter`
2. `ExecutionPriorityResolver`
3. `ActionConflictClassifier`
4. `ActionSuppressionPolicy`
5. `FlattenPrivilegeResolver`

#### 标准 Contract

1. `action_id`
2. `action_type`
3. `priority`
4. `suppressed`
5. `suppression_reason`
6. `winning_action_set`
7. `blocked_action_set`

#### 开发 Todo

1. 统一定义 `flatten > protection > reduce > entry > add`
2. 明确同一 tick 多 action 冲突时只允许一个 winning set
3. 明确哪些 action 可以并发，哪些必须互斥
4. 将 action suppression 审计标准化

#### 测试点

1. `flatten` 与 `entry` 同时出现
2. `protection self-heal` 与 `take profit` 同时出现
3. `recovery flatten` 与 `trailing stop` 同时出现
4. 多 action 相同优先级时排序稳定

#### 验收点

1. 所有 action 冲突都经过统一仲裁
2. 不允许多个模块各自直接下单抢控制权

### 4.1 入场执行模块

#### 子模块

1. `MarketEntryExecutor`
2. `LimitEntryExecutor`
3. `BreakoutEntryExecutor`
4. `PullbackEntryExecutor`
5. `ScaleInEntryExecutor`
6. `ReentryCooldownController`

#### 标准 Contract

1. `entry_style`
2. `order_style`
3. `timeout_sec`
4. `cooldown_sec`
5. `slippage_limit`
6. `exec_symbol_policy`

#### 开发 Todo

1. entry executor 与 signal 解耦
2. timeout/cancel/cooldown 标准化
3. trusted confirmation 标准化

#### 测试点

1. market entry
2. limit entry
3. timeout cancel
4. cooldown 生效

#### 验收点

1. 交易员可以独立更换 entry execution 风格

### 4.1.1 订单生命周期 / Broker 真相同步模块

#### 子模块

1. `OrderLifecycleStateMachine`
2. `BrokerTruthSync`
3. `TrustedFillConfirmation`
4. `TerminalStateResolver`
5. `GhostOrphanMismatchClassifier`
6. `OrderFreezeController`

#### 标准 Contract

1. `order_ref`
2. `broker_order_id`
3. `perm_id`
4. `order_lifecycle_state`
5. `broker_truth_state`
6. `trusted_fill`
7. `trusted_terminal`
8. `sync_required`
9. `freeze_required`

#### 开发 Todo

1. 明确 `created / submitted / accepted / partial_fill / filled / cancelled / rejected / expired`
2. 明确 `accepted != filled`
3. 引入 broker truth refresh 后再 terminalize 的标准动作
4. 对 `ghost/orphan/mismatch` 增加统一 freeze 规则
5. 订单生命周期与 lot lifecycle 明确解耦又可关联

#### 测试点

1. broker ack 但未 fill
2. partial fill 多次回报
3. terminal update 重复到达
4. broker 有单 DB 无单
5. DB 有单 broker 无单
6. sync 失败时 freeze 生效

#### 验收点

1. 订单状态转换有唯一状态机
2. broker 真相高于 DB 投影与本地缓存
3. terminal orphan 不会在脏状态下继续开仓

### 4.2 持仓生命周期模块

#### 子模块

1. `PendingFillState`
2. `LiveState`
3. `PostTP1State`
4. `PendingExitState`
5. `ClosedState`
6. `QuarantineState`

#### 标准 Contract

1. `position_lot_id`
2. `lifecycle_state`
3. `filled_qty`
4. `live_qty`
5. `avg_entry_price`
6. `tp1_hit`
7. `quarantine_reason`

#### 开发 Todo

1. lot lifecycle 标准化
2. 状态机转移表固化
3. quarantine 与 recovery 挂钩

#### 测试点

1. pending -> live
2. live -> post_tp1
3. live -> pending_exit
4. terminal -> closed

#### 验收点

1. 生命周期可重放、可解释

## 5. 保护与收益管理域详细拆解

### 5.1 止损保护单模块

#### 子模块

1. `InitialProtectionBuilder`
2. `ProtectionAttachController`
3. `ProtectionAuditLoop`
4. `ProtectionSelfHeal`
5. `RestartProtectionRebuilder`
6. `UnsafePositionFallbackFlatten`

#### 标准 Contract

1. `protection_required`
2. `stop_type`
3. `stop_price`
4. `protection_state`
5. `audit_status`
6. `self_heal_count`

#### 开发 Todo

1. protection invariant 平台化
2. position-attached protection 生命周期
3. restart rebuild 策略模板化

#### 测试点

1. entry attach
2. missing stop detect
3. self-heal
4. rebuild
5. rebuild fail -> flatten

#### 验收点

1. live position 永远不会裸奔

### 5.2 止盈 / TP1 / Trailing 模块

#### 子模块

1. `TP1Policy`
2. `TP2Policy`
3. `TrailingAnchor`
4. `TrailingFloor`
5. `BreakevenShield`
6. `DynamicStopAdjust`

#### 标准 Contract

1. `tp1_enabled`
2. `tp1_qty_ratio`
3. `tp1_trigger`
4. `trailing_mode`
5. `trailing_anchor_px`
6. `trailing_floor_px`
7. `breakeven_armed`

#### 开发 Todo

1. 部分止盈标准化
2. trailing state 平台化
3. breakeven / dynamic stop contract 化

#### 测试点

1. TP1 partial
2. trailing anchor update
3. trailing stop trigger
4. breakeven arm/disarm

#### 验收点

1. 交易员可以配置完整收益管理链

## 6. 平仓与恢复域详细拆解

### 6.1 平仓 / Flatten 控制模块

#### 子模块

1. `EODFlattenController`
2. `PanicFlattenController`
3. `RecoveryFlattenController`
4. `ManualFlattenController`
5. `FlattenCompletionVerifier`

#### 标准 Contract

1. `flatten_reason`
2. `flatten_scope`
3. `close_only`
4. `cancel_before_flatten`
5. `completion_confirmed`

#### 开发 Todo

1. flatten 原因分层
2. flatten 前后 cleanup 标准化
3. flatten completion confirmation

#### 测试点

1. EOD flatten
2. panic flatten
3. recovery flatten
4. completion verify

#### 验收点

1. 平仓逻辑绝对优先且结果可确认

### 6.2 恢复 / 重启模块

#### 子模块

1. `StartupPreflight`
2. `RuntimeRecoveryGate`
3. `BrokerStateRecovery`
4. `DBProjectionRecovery`
5. `PendingStateRecovery`
6. `ProtectionRecovery`

#### 标准 Contract

1. `startup_ok`
2. `runtime_clean`
3. `broker_truth_ready`
4. `db_projection_ready`
5. `recovery_mode`
6. `recovery_actions`

#### 开发 Todo

1. startup gate 分层
2. degraded-open / hard-block 模板化
3. recovery mode action 标准化

#### 测试点

1. broker 有仓 / DB 无仓
2. DB 有单 / broker 无单
3. protection 缺失恢复
4. startup blocked -> recovered

#### 验收点

1. 重启后系统先恢复，再交易

## 7. 组合与运维域详细拆解

### 7.1 组合暴露 / 跨策略约束模块

#### 子模块

1. `PortfolioExposurePolicy`
2. `StrategyOwnershipPolicy`
3. `FlattenAuthorityPolicy`
4. `ProtectionAuthorityPolicy`
5. `CrossStrategyIsolationPolicy`

#### 标准 Contract

1. `ownership_scope`
2. `allowed_position_scope`
3. `flatten_authority`
4. `protection_authority`
5. `exposure_limit`

#### 开发 Todo

1. 明确多策略 ownership
2. 明确谁能 flatten
3. 明确谁能挂保护单

#### 测试点

1. 同 symbol 双策略不串仓
2. foreign order 不越权
3. flatten authority 生效

#### 验收点

1. 多策略共存不互相踩踏

### 7.2 恢复 / 审计 / 回放模块

#### 子模块

1. `TraderAuditSchema`
2. `DecisionReplay`
3. `ExecutionReplay`
4. `StrategyExplainability`
5. `RegressionMatrixRunner`

#### 标准 Contract

1. `why_enter`
2. `why_block`
3. `why_exit`
4. `why_flatten`
5. `replay_trace_id`

#### 开发 Todo

1. trader-facing explanation 标准化
2. replay 输入输出标准化
3. regression matrix product 化

#### 测试点

1. 单笔交易 explain
2. replay 可还原 action chain
3. regression matrix 自动跑关键模板

#### 验收点

1. 交易员能看懂一笔交易为什么这样做

### 7.3 执行质量 / 滑点归因模块

#### 子模块

1. `ExecutionQualityRecorder`
2. `SlippageAttribution`
3. `FillLatencyTracker`
4. `TriggerVsFillComparator`
5. `FlattenCostRecorder`

#### 标准 Contract

1. `trigger_price`
2. `expected_price`
3. `fill_price`
4. `slippage_bps`
5. `fill_latency_ms`
6. `partial_fill_ratio`
7. `flatten_cost_bps`
8. `execution_quality_grade`

#### 开发 Todo

1. 记录 trigger -> submit -> ack -> fill 全链路时间
2. 记录理论价格与实际 fill 偏差
3. 区分 entry slippage / exit slippage / flatten slippage
4. 将执行质量纳入 trader-facing explainability

#### 测试点

1. 快市下 fill latency 记录正确
2. partial fill 归因正确
3. flatten fallback 成本可解释
4. slippage 指标不会丢 trace_id / order_ref

#### 验收点

1. 交易员能判断 alpha 是被执行吃掉还是策略本身失效
2. execution quality 指标可用于回测-实盘偏差分析

## 8. 推荐交付顺序

### Wave 1: 必须先做

1. 实时行情 readiness contract
2. gate contract 升级
3. sizing 独立模块
4. DecisionLock
5. protection invariant
6. ActionArbiter
7. OrderLifecycle / BrokerTruthSync

#### Wave 1 硬阈值

1. `pending entry` 默认必须在 `<= 60s` 内进入 trusted fill 或 trusted terminal 路径。
2. stale pending entry 默认必须在 `<= 75s` 内进入 cancel/quarantine/cooldown 收敛。
3. live position 缺 protection 默认必须在 `<= 10s` 检测到，并在 `<= 30s` 内 self-heal 或 flatten/freeze。
4. ghost/orphan/mismatch 出现后，相关 lane 的新 entry 必须在下一次 action arbitration 前被冻结。
5. flatten 发出后 broker/DB/runtime flat 的默认验收阈值为 `<= 90s`。
6. sizing 与 reservation / protection qty 偏差容忍度为 `0`。

### Wave 2: 核心策略能力

1. PositionLifecycle
2. TP1 partial exit
3. trailing stop
4. flatten controller
5. restart recovery
6. ExecutionQuality / SlippageAttribution

### Wave 3: 平台化增强

1. Universe / Instrument / Lane 完整定义
2. portfolio exposure / ownership
3. replay / explainability
4. full regression matrix

## 9. 一句话执行原则

每个大模块都必须继续拆成“小模块 + contract + 测试 + 验收”，否则所谓“可插拔”最后还是会退化成把所有策略细节堆回 `strategy.py`。
