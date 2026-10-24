# Stocks Kernel First-Principles Master Plan

目的：从第一性原理出发，定义 `stocks_kernel` 如何演进成一个真正模块化、可插拔、可多策略并行、可同票多策略共存的股票策略框架。  
目标用户：专业量化交易员、策略研究员、执行与风控工程师。  
核心要求：交易员不改 kernel 主骨架，只通过“模块 + 配置 + contract”像搭积木一样组合出任意股票策略。

## 0.1 当前落地状态

这份文档不是静态 roadmap，下面是当前代码状态与下一步边界。

### 已落地

1. Phase 0：
   lane / ownership / authority / action / lifecycle / audit 基础 contract 已进入代码。
2. Phase 1：
   `DataContextBundle` 已进入运行时，`signal / execution / confirm / hedge` 多域上下文已能在 strategy runtime 中补齐。
3. Phase 2：
   evaluator 已输出 `action_candidates / winning_actions / blocked_actions` contract。
4. Phase 3：
   entry / exit / protection / startup recovery 已接入 ownership / authority 的第一层硬约束。
5. Phase 4：
   `RuntimeOrchestrator` 已从 scaffold 升级为主链路 stage executor。
6. Phase 6：
   portfolio exposure 与 same-symbol ownership 已接入 entry gate。
7. Phase 7：
   replay / explain / certification harness 已存在并通过回归。

### 部分落地

1. Phase 1：
   多域数据已进入 runtime，但 feature/gate 还没有系统性消费 `execution / confirm / hedge`。
2. Phase 4：
   stage graph 已驱动主链路，但 action set 仍未完整支持“一个 tick 多动作协同执行”。
3. Phase 5：
   protection / flatten / recovery 已有约束与 watchdog，但 TP1 / trailing / breakeven / dynamic stop 仍未完整 state machine 化。
4. Phase 6：
   multi-strategy 账户级隔离已起步，但 authority 仍需继续深入 runtime safety/reconcile/adopt。

### 未落地

1. 多域 feature/gate 标准消费接口
2. TP1 / trailing / breakeven / dynamic stop 完整生命周期
3. action set 协同执行图
4. adopt/reconcile 的 authority-scoped 正式模板
5. 同票多策略 integration / e2e matrix

### 当前执行原则

1. 所有新增能力默认向后兼容
2. 不直接替换旧策略路径，只做 contract-first 接入
3. 任何新增自动化动作必须先过 ownership / authority
4. 无 supplemental data 时必须 fail-soft，而不是打断主策略

## -1. Review + Critical

这份计划原先的大方向是对的，但有 5 个需要收紧的地方，否则会在实施阶段重新退化：

1. 只讲目标，没有明确“不允许怎么做”  
   如果不明确禁止项，实施时会再次回到：
   - 往 `strategy.py` 继续塞流程
   - 往 `extras` 继续塞策略私货
   - 为某一个样本策略写 kernel 特判

2. 只讲模块，没有明确兼容约束  
   真实系统不是白纸，必须明确：
   - 默认行为不能破坏现有策略
   - 新 contract 必须向后兼容
   - 旧策略可以不立即迁移

3. 只讲 phase，没有灰度与回滚设计  
   如果没有 feature flag、fallback、rollback gate，再好的架构也会在上线时带来新风险。

4. 只讲“能做什么”，没有明确“先做到什么程度算可以上线”  
   平台化是长期目标，但每一阶段都必须有最小上线闭环，不然会变成无限重构。

5. 只讲功能模块，没有明确 correctness gate  
   交易系统里，任何 phase 的上线标准都必须先满足：
   - 不错方向
   - 不重复发单
   - 不假持仓
   - 不裸仓
   - 不把别的策略仓位当成自己的

基于这些问题，后面的计划必须按：

1. 先 contract
2. 再 compatibility
3. 再 rollout
4. 再 feature migration
5. 最后才是“更优雅的架构”

## -0. 非目标

下面这些不是当前实施目标，必须明确排除：

1. 不是一次性替换现有所有策略
2. 不是为了复刻单个样本策略而写死 kernel
3. 不是追求“理论最优架构”而牺牲现有系统稳定性
4. 不是重写 broker / DB / execution service 全链路
5. 不是要求旧配置立即废弃

## 0. 第一性原理

先不讨论 `QQQ`、`SOXL`、`SOXS` 或任何具体策略。  
从交易员真正要做的事情看，一个股票策略平台必须先解决 6 个根问题：

1. 交易员如何定义“看什么”
2. 交易员如何定义“算什么”
3. 交易员如何定义“什么时候能做”
4. 交易员如何定义“做什么动作”
5. 交易员如何定义“怎么执行与怎么保护”
6. 系统如何保证“多策略同时运行也不会串仓、串单、乱平仓”

如果这 6 个问题没有作为平台 contract 固化，系统再多模块也只是“更大的策略脚本”。

所以平台最终必须满足：

1. 数据是模块
2. 特征是模块
3. 信号是模块
4. 门控是模块
5. 执行动作是模块
6. 仓位生命周期是模块
7. 保护与收益管理是模块
8. 平仓与恢复是模块
9. ownership / authority / lane 是硬约束
10. broker truth / audit / replay 是底层基础设施

## 1. 最终要达到的产品形态

交易员要能组合下面这些积木，而不是去改 `strategy.py`：

1. 实时行情数据源
2. 历史行情数据源
3. 实时特征
4. 历史特征
5. regime / session / market-state
6. universe / instrument / lane
7. signal
8. gate
9. sizing
10. entry executor
11. add / reduce executor
12. stop / protection
13. TP1 / TP2 / trailing / breakeven
14. flatten / recovery / panic close
15. portfolio exposure / cross-strategy isolation
16. audit / replay / explainability

并且要支持 4 类运行形态：

1. 单策略单票
2. 单策略多票
3. 多策略异票并行
4. 多策略同票并行

## 2. 顶层架构原则

### 2.1 单一真相源原则

1. broker open orders / broker positions 是外部执行真相
2. DB 是内部账本与审计真相
3. strategy runtime / ctx 只是缓存
4. 任何自动修复都必须遵守：先 broker，后 DB，最后 runtime

### 2.2 lane 优先于 symbol 原则

平台最小原子不能是 `symbol`，必须是 `lane`。

每一条 lane 至少要定义：

1. `lane_id`
2. `lane_role`
3. `symbol`
4. `side_semantic`
5. `ownership_scope`
6. `flatten_authority`
7. `protection_authority`
8. `execution_preferences`

否则同票多策略永远只能靠猜。

### 2.3 action set 优先于单 action 原则

一个 tick 内，系统要能表达的是：

1. action candidates
2. winning action set
3. blocked action set
4. suppression reason

而不是“一个 signal -> 一个 entry_plan”。

### 2.4 position-attached protection 原则

保护单保护的是仓位，不是某次下单事件。

所以：

1. protection 绑定 `position_lot_id`
2. restart 后必须可重建
3. 持仓存在但 protection 缺失时，系统必须立即进入降级路径

### 2.5 flatten 是特权路径

无论是：

1. EOD flatten
2. panic flatten
3. recovery flatten
4. manual flatten

都必须高于 entry / add / TP / retreat。

### 2.6 多策略同票必须先定义权限边界

平台必须先回答：

1. 谁拥有这条 lane
2. 谁能开仓
3. 谁能减仓
4. 谁能挂保护单
5. 谁能 flatten
6. 谁能 adopt/reconcile broker residual

否则“支持多策略同票”是危险口号，不是能力。

### 2.7 默认向后兼容原则

任何新 contract 落地都必须满足：

1. 默认值不改变旧策略行为
2. 旧配置不需要立即迁移
3. 新字段只增强，不破坏现有语义
4. 没启用新模块时，运行路径与旧逻辑等价

### 2.8 灰度优先原则

每个 phase 的交付都必须支持：

1. feature flag
2. partial rollout
3. per-strategy enablement
4. fallback to old path
5. rollback without DB surgery

## 3. 核心 contract 清单

### 3.1 数据域 contract

#### 实时行情 contract

1. `signal_snapshot`
2. `exec_snapshot_map`
3. `confirm_snapshot_map`
4. `hedge_snapshot_map`
5. `freshness_ok`
6. `quote_ready`
7. `trade_ready`
8. `session`
9. `snapshot_source`

#### 历史行情 contract

1. `signal_history`
2. `exec_history_map`
3. `gap_count`
4. `largest_gap_sec`
5. `continuity_ok`
6. `warmup_ready`

#### 实时特征 contract

1. `feature_id`
2. `scope`
3. `value`
4. `ready`
5. `criticality`
6. `max_age_sec`
7. `degraded`

#### 历史特征 contract

1. `seed_state`
2. `anchor_state`
3. `opening_window_state`
4. `historical_feature_version`
5. `replay_parity_ok`

### 3.2 策略表达 contract

#### Signal contract

1. `signal_id`
2. `signal_context`
3. `direction`
4. `strength`
5. `confidence`
6. `action_candidates`
7. `inhibition_reasons`

#### Gate contract

1. `gate_id`
2. `allowed`
3. `severity`
4. `recoverable`
5. `block_scope`
6. `retry_after_sec`
7. `payload`

#### Sizing contract

1. `sizing_mode`
2. `planned_qty`
3. `planned_notional`
4. `risk_unit`
5. `capital_limit_usd`
6. `reservation_hint`
7. `per_lane_budget`

### 3.3 执行 contract

#### Action contract

1. `action_type`
2. `lane_id`
3. `symbol`
4. `side`
5. `qty`
6. `priority`
7. `position_lot_id`
8. `close_only`
9. `reduce_only`
10. `flatten_action`
11. `protection_attached`
12. `reason`
13. `metadata`

#### Order lifecycle contract

1. `created`
2. `submitted`
3. `accepted`
4. `partial_fill`
5. `filled`
6. `cancelled`
7. `rejected`
8. `expired`
9. `sync_required`
10. `freeze_required`

#### Position lifecycle contract

1. `intent_open`
2. `pending_fill`
3. `live`
4. `post_tp1`
5. `pending_exit`
6. `closed`
7. `quarantined`

### 3.4 权限与组合 contract

#### Lane ownership contract

1. `ownership_scope`
2. `strategy_namespace`
3. `allowed_position_scope`
4. `flatten_authority`
5. `protection_authority`
6. `adopt_authority`

#### Portfolio contract

1. `gross_exposure_limit`
2. `net_exposure_limit`
3. `symbol_exposure_limit`
4. `strategy_exposure_limit`
5. `cross_strategy_conflict_policy`

### 3.5 审计 contract

每笔单必须至少有：

1. `strategy_id`
2. `lane_id`
3. `symbol`
4. `decision_id`
5. `trace_id`
6. `order_ref`
7. `position_lot_id`
8. `broker_order_id`
9. `perm_id`
10. `intent_action`
11. `close_only`
12. `flatten_action`
13. `status`
14. `reason`
15. `qty`
16. `price`

## 4. 总体升级 Plan

### Phase 0: Contract Freeze

目标：先把平台语言固定下来。

Todo：

1. 固化 lane / ownership / authority / action / lifecycle / audit 统一 contract
2. 停止新增策略专有 extras 逃逸字段
3. 形成 contract compatibility checklist

测试计划：

1. contract schema unit tests
2. config parse tests
3. backward-compat parse tests

验收标准：

1. 任何新增策略能力都能落在已有 contract
2. 新增字段必须说明 scope / producer / consumer / audit path
3. 所有 contract 变更默认向后兼容

### Phase 1: Data Plane 重构

目标：让数据成为交易语义模块，而不是一份混合上下文。

Todo：

1. 引入 `SignalDataContext`
2. 引入 `ExecutionDataContext`
3. 引入 `ConfirmDataContext`
4. 引入 `HedgeDataContext`
5. 区分 signal / exec / confirm / hedge 的 snapshot readiness
6. 区分 signal / exec / confirm / hedge 的 history readiness
7. 支持 multi-symbol feature computation
8. 增加 continuity / gap / session boundary 标准判断

测试计划：

1. signal ready but exec stale
2. signal stale but exec ready
3. confirm leg missing
4. hedge leg missing
5. session boundary gap
6. warmup replay parity

验收标准：

1. 数据 contract 不再假设“单 symbol 一份上下文”
2. entry 决策依赖 exec leg 时，不允许退化成 signal leg 盲下单
3. 旧策略在未启用多数据域 contract 时保持原行为

### Phase 2: Strategy Expression 重构

目标：让交易员表达的是“动作意图集合”，不是一条 entry。

Todo：

1. signal contract 升级为 action candidates
2. evaluator 输出 `candidates / winning / blocked`
3. gate 输出 `severity / block_scope / recoverable`
4. sizing 升级为独立模块链
5. policy 不再直接绑定 `build_entry_plan` 为核心入口

测试计划：

1. single-entry candidate
2. entry + add candidates
3. flatten + cancel candidates
4. blocked candidate explainability
5. second strategy reusing same signal/gate contract

验收标准：

1. 策略作者不需要改主骨架就能新增新 signal/gate/sizer
2. 同一 tick 可以稳定输出多个候选动作
3. 旧 policy contract 仍可通过兼容层运行

### Phase 3: Lane / Ownership / Authority Plane

目标：真正支持同票多策略、多 lane 共存。

Todo：

1. `KernelInstrumentBinding` 升级为 `InstrumentDefinition`
2. 增加 `signal_lanes / execution_lanes / protection_lanes / hedge_lanes`
3. 在 `PositionLot`、`LotOrderBinding`、audit 中补 `lane_id`
4. 增加 `OwnershipPolicy`
5. 增加 `FlattenAuthorityPolicy`
6. 增加 `ProtectionAuthorityPolicy`
7. 增加 broker adopt authority contract

测试计划：

1. same symbol two strategies isolated lots
2. same symbol two strategies different lanes
3. only one strategy has flatten authority
4. only one strategy has protection authority
5. orphan order adoption scoped by authority

验收标准：

1. 同票多策略运行时不会因为 symbol 相同而互相覆盖
2. 平仓、保护、adopt 都有显式权限控制
3. 未声明 authority 的旧策略默认退化为 `strategy-scoped`

### Phase 4: Execution Graph 重构

目标：从巨石 strategy shell 变成模块运行图。

Todo：

1. 定义 `RuntimeOrchestrator`
2. 定义模块依赖图与执行顺序 contract
3. 将 `session / recovery / action arbitration / protection / flatten / broker truth sync` 从 `strategy.py` 下沉为 graph node
4. 支持 action set 执行，而不是命中第一笔就 return
5. 支持 `cancel_working + flatten`
6. 支持 `tp1 + re-arm stop`
7. 支持 `reduce + trailing adjust`

测试计划：

1. graph order deterministic
2. one tick multiple winning actions
3. node failure isolation
4. action suppression deterministic

验收标准：

1. 新模块接入只需要注册 graph node
2. 不需要继续往 `ComposableStocksKernelStrategy` 塞控制流
3. graph runtime 可以逐策略灰度切换

### Phase 5: Protection / TP / Flatten / Recovery 完整化

目标：完整表达仓位管理链。

Todo：

1. position-attached protection state machine
2. restart protection rebuild
3. TP1 partial exit contract
4. TP2 / trailing / breakeven / dynamic stop contract
5. `unsafe_position_without_protection -> flatten_only/freeze`
6. flatten completion verifier 与 escalation policy
7. recovery mode action templates

测试计划：

1. entry -> stop attached
2. restart -> stop rebuild
3. TP1 partial then trailing
4. flatten timeout escalate
5. missing protection self-heal then flatten

验收标准：

1. live position 不会裸奔
2. flatten 不会卡在“已发单但未收敛”状态下继续正常交易
3. protection / flatten / recovery 任一模块关闭时，旧路径仍可运行

### Phase 6: Portfolio / Multi-Strategy Plane

目标：支持账户级多策略生产运行。

Todo：

1. portfolio exposure policy
2. cross-strategy conflict policy
3. per-symbol per-lane budget
4. shared account inventory awareness
5. strategy namespace isolation
6. multi-strategy audit aggregation

测试计划：

1. multi-strategy same account
2. multi-strategy same symbol
3. one strategy blocked, others continue
4. portfolio gross limit reached

验收标准：

1. 多策略并行运行时，阻断和风险控制是 scoped 的
2. 某一策略异常不会把整个账户 runtime 拖入错误状态
3. 同票多策略不再依赖 symbol 模糊隔离

### Phase 7: Replay / Explainability / Acceptance Harness

目标：让交易员和运维都能直接复盘。

Todo：

1. replay API
2. decision explain API
3. execution explain API
4. trader-facing audit schema
5. regression matrix runner
6. strategy certification harness

测试计划：

1. replay one trade
2. replay one session
3. explain blocked entry
4. explain wrong-way order suppression
5. explain protection self-heal escalation

验收标准：

1. 交易员能直接看到为什么进、为什么没进、为什么平、为什么 freeze
2. 任何事故都能从 replay/audit 还原链路

## 4.1 发布红线

任何 phase 交付，如果触发下面任一项，不允许进入生产：

1. 同一策略重复下单
2. 同一 symbol 跨策略串仓
3. `accepted` 被错误视为 `filled`
4. live position 丢失 protection 后继续正常交易
5. flatten 与 entry 在同一 lane 竞争
6. 新模块启用后无法快速回退

## 4.2 灰度策略

所有大改动必须支持：

1. `global_disabled`
2. `strategy_enabled`
3. `template_enabled`
4. `shadow_mode`
5. `audit_only`

顺序要求：

1. 先 `audit_only`
2. 再 `shadow_mode`
3. 再单策略启用
4. 最后模板级推广

## 5. 详细 Todo List

### 5.1 必做 P0

1. 给 `PositionLot` 增加 `lane_id`
2. 给 `LotOrderBinding` 增加 `lane_id`
3. 给 audit 增加 `lane_id`
4. 给 config 增加 ownership / authority 配置
5. 拆出 `DataContextBundle`
6. evaluator 改为多上下文输入
7. action execution 改成 action set
8. strategy shell 改成 runtime orchestrator

### 5.2 必做 P1

1. TP1 partial exit state machine
2. trailing / breakeven / dynamic stop contract
3. flatten controller
4. recovery controller
5. portfolio exposure policy

### 5.3 必做 P2

1. replay service
2. explainability service
3. strategy certification harness
4. production checklist generator

## 6. 测试总计划

### 6.1 Unit Tests

1. contract schema
2. config parse
3. lane resolution
4. ownership checks
5. gate semantics
6. action arbitration
7. order lifecycle
8. position lifecycle
9. protection lifecycle
10. flatten convergence

### 6.2 Integration Tests

1. signal leg != exec leg
2. single strategy multi-lane
3. same symbol multi-strategy
4. broker truth mismatch
5. restart recovery
6. EOD flatten

### 6.3 End-to-End Tests

1. one full trade lifecycle
2. TP1 -> trailing -> flatten lifecycle
3. orphan/ghost recovery lifecycle
4. same account two strategies lifecycle
5. same symbol two strategies lifecycle

### 6.4 Regression Matrix

至少要覆盖：

1. 单标的趋势策略
2. 单标的均值回归策略
3. 代理 ETF 映射策略
4. 同标的执行策略
5. 双执行腿策略
6. 多策略同票并行

## 7. 验收标准清单

### 7.1 平台能力验收

1. 新策略接入主要通过配置与模块，不改主骨架
2. 主骨架不再包含策略专名分支
3. 平台 contract 可以同时表达单腿、双腿、同标的、多 lane 策略

### 7.2 交易员体验验收

1. 交易员可以独立替换数据模块
2. 交易员可以独立替换特征模块
3. 交易员可以独立替换 signal / gate / sizing / executor / protection / flatten 模块
4. 交易员可以组合出第二个风格明显不同的策略而不改 kernel

### 7.3 正确性验收

1. `accepted != filled` 全链路成立
2. live position 永远满足 protection invariant
3. flatten 绝对高于 entry / add / reduce / TP 优化
4. broker / DB / runtime 最终一致
5. ghost / orphan / mismatch 不会在脏状态下继续新 entry

### 7.4 多策略验收

1. 多策略异票并行稳定
2. 多策略同票并行稳定
3. 平仓权限明确，不会反向平别人的仓
4. 保护权限明确，不会给别人的 lot 挂保护
5. adopt/reconcile 权限明确，不会错误接管

### 7.5 运维验收

1. 盘前可验证 broker/DB/runtime 清洁度
2. 盘中可直接定位 blocked/freeze/flatten/protection 问题
3. 收盘后可验证所有策略是否正确收口
4. 事故后可从 replay + audit 完整还原链路

## 8. Done 定义

只有当下面 8 条同时成立，才能说这个框架完全满足需求：

1. 交易员可以不改 kernel 主骨架拼装新策略
2. 平台能表达多数据域、多 lane、多动作集合
3. 平台能表达完整 entry / add / reduce / TP / stop / flatten 链
4. 平台能支持同票多策略并发且不串仓串单
5. 平台能保证 broker truth 优先与最终一致
6. 平台能保证 protection invariant 与 flatten 特权路径
7. 平台能给出 trader-facing explainability
8. 平台能通过 regression matrix 证明它不是只适配一个样本策略

## 9. 一句话结论

你要的不是“一个可配置策略壳”，而是“一个把数据、特征、信号、门控、执行、保护、平仓、组合权限全部 contract 化的交易操作系统”。  
后续所有开发都必须以这个标准推进，否则可插拔会再次退化成巨石策略。
