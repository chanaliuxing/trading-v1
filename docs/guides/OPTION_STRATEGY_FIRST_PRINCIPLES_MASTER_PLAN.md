# Option Strategy First-Principles Master Plan

目的：从第一性原理出发，定义本仓库的 option 策略平台如何演进成一个真正可插拔、可组合、可多策略并行、可同标的多策略共存的统一框架。  
目标用户：专业期权交易员、策略研究员、执行与风控工程师。  
核心要求：交易员不改 kernel 主骨架，只通过“模块 + 配置 + contract”像搭积木一样组合出新的 option 策略。

## 0. 第一性原理

先不讨论 `QQQ`、`SPY` 或任何一个现有策略名。  
从交易员真正要解决的问题看，一个 option 策略平台必须先回答 7 个根问题：

1. 交易员如何定义市场方向与入场时机
2. 交易员如何把方向信号映射成期权表达
3. 交易员如何选择合约，而不是把合约选择硬编码在策略里
4. 交易员如何定义入场、止损、止盈、平仓与恢复
5. 交易员如何定义仓位与资金占用
6. 系统如何保证多策略并行时不串仓、不串单、不乱 flatten
7. 系统如何证明一个新策略在 replay、shadow、live acceptance 上是安全的

如果这 7 个问题没有被平台 contract 固化，系统再多插件也只是“多个脚本挂在一个公共壳上”。

所以平台最终必须满足：

1. signal 是模块
2. contract selection 是模块
3. option direction mapping 是模块
4. execution policy 是模块
5. position lifecycle 是模块
6. protection / TP / trailing / flatten 是模块
7. capital / exposure / ownership 是硬约束
8. broker truth / audit / replay / acceptance 是底层基础设施
9. strategy definition 是唯一组装入口
10. `QQQ` 等稳定现网策略是验收标尺，不是实验对象

## 1. 最终要达到的产品形态

交易员要能组合下面这些积木，而不是去改 `strategy.py` 或复制一个新策略类：

1. underlying market signal module
2. option-side direction mapping module
3. contract universe / contract filter module
4. DTE / delta / liquidity policy
5. quote readiness / stale quote gate
6. entry execution policy
7. add / reduce / re-entry policy
8. position sizing / capital reservation policy
9. hard stop / logic retreat policy
10. TP1 / TP2 / trailing / breakeven policy
11. EOD flatten / panic flatten / recovery flatten policy
12. protection rebuild / self-heal policy
13. broker sync / order lifecycle / adoption policy
14. state machine / audit / replay / explainability layer
15. rollout / shadow / acceptance policy

并且要支持 4 类运行形态：

1. 单策略单标的
2. 单策略多合约表达
3. 多策略异标的并行
4. 多策略同 underlying 并行

## 2. 顶层架构原则

### 2.1 Strategy Definition 是唯一组装入口

任何 option 策略都必须先表达成一个 `StrategyDefinition`，至少包含：

1. `strategy_family`
2. `strategy_id`
3. `underlying`
4. `signal_module`
5. `direction_mapping_policy`
6. `contract_policy`
7. `execution_policy`
8. `position_policy`
9. `risk_policy`
10. `flatten_policy`
11. `audit_namespace`

插件只负责 assembly，不负责重新定义一套私有语义。

### 2.2 交易决策与期权执行分层

平台必须区分两类真相：

1. 市场方向真相  
例如 `QQQIntradayStrategy` 的 underlying 方向判断。
2. 期权执行真相  
例如 delta、DTE、spread、quote freshness、fill、slippage。

方向层决定“是否值得做”。  
期权层决定“用哪张合约、以什么方式做、何时安全退出”。

### 2.3 合约选择不能是策略私货

真正的期权平台不能把下面这些散落在各策略实现里：

1. `preferred_dte`
2. `fallback_dte`
3. `delta_band`
4. `spread_limit`
5. `oi/liquidity filters`
6. `quote_age limits`

这些必须成为独立 contract policy，否则无法证明两个策略到底是“同一逻辑不同标的”还是“看起来像同一逻辑”。

### 2.4 状态机与执行链必须统一

option 平台最终必须有统一状态表达：

1. runtime lifecycle
2. order lifecycle
3. flatten lifecycle
4. protection lifecycle

策略只声明意图和规则，不再自行发明私有状态语义。

### 2.5 ownership / authority 优先于 symbol

同一个 underlying 允许多策略并行时，最小原子不能是 `symbol`，必须是：

1. `strategy_id`
2. `risk_symbol`
3. `ownership_scope`
4. `flatten_authority`
5. `protection_authority`
6. `capital_group`

否则“多策略同票”只是偶然没撞上冲突。

### 2.6 QQQ stable path freeze

`QQQIntradayStrategy` 与已验证稳定的 stock-side 逻辑必须被冻结为验收基线：

1. 不随平台重构直接改交易逻辑
2. 新 option parity 只能先 shadow / replay / acceptance
3. 任何新平台能力先证明“不破坏 QQQ”，再允许推广

## 3. 核心 contract 地图

### 3.1 Signal Contract

1. `signal_id`
2. `signal_family`
3. `underlying`
4. `direction`
5. `strength`
6. `confidence`
7. `signal_context`
8. `inhibition_reasons`
9. `entry_window_state`

### 3.2 Direction Mapping Contract

1. `stock_long -> call`
2. `stock_short -> put`
3. `call/put disable rules`
4. `direction-specific filters`
5. `mapping_reason`

### 3.3 Contract Selection Contract

1. `preferred_dte`
2. `fallback_dte_max`
3. `target_delta_band`
4. `max_spread_width`
5. `quote_freshness_sec`
6. `open_interest_min`
7. `liquidity_pass`
8. `stale_quote_policy`
9. `selection_reason`

### 3.4 Execution Contract

1. `entry_style`
2. `close_style`
3. `timeout_sec`
4. `cancel_retry_policy`
5. `cooldown_policy`
6. `partial_fill_policy`
7. `degraded_close_policy`
8. `slippage_guard`

### 3.5 Position / Risk Contract

1. `sizing_mode`
2. `planned_contract_qty`
3. `planned_notional`
4. `capital_limit_usd`
5. `entry_underlying_price`
6. `stop_anchor_type`
7. `logic_retreat_anchor`
8. `eod_flatten_cutoff`
9. `panic_flatten_rule`

### 3.6 State / Ownership Contract

1. `strategy_id`
2. `risk_symbol`
3. `audit_namespace`
4. `ownership_scope`
5. `flatten_authority`
6. `protection_authority`
7. `runtime_state_machine`
8. `order_state_machine`
9. `flatten_state_machine`
10. `protection_state_machine`

### 3.7 Acceptance Contract

1. replay parity
2. shadow readiness
3. shadow acceptance
4. cutover block / allow
5. rollback path
6. operator evidence output

## 4. 当前架构判断

### 4.1 已有基础

目前已经有这些正确方向的基础设施：

1. option plugin kernel
2. strategy definition registry
3. shared contract / execution / position policy 基础模块
4. `QQQ` shadow state acceptance pipeline
5. state machine / audit / readiness / acceptance 基础设施
6. option strategy inventory / assembly-adapter compliance / platform coverage 报告链

### 4.2 还没有完全达到“任意 option 策略像搭积木一样组装”

还差的不是再加几个插件文件，而是缺这几层完全产品化：

1. 交易员视角的标准模块地图还没彻底固定
2. definition schema 还没强到能防止 plugin 私货漂移
3. signal / contract / execution / risk / flatten 的边界还不够硬
4. trader-facing schema / template / checklist 还没完全产品化
5. option 平台级 replay / shadow / cutover gate 还没统一收口

所以当前结论必须准确：

1. 不是“已经完全任意 option 策略积木化”
2. 是“已经有内核和验收骨架，可以继续收口成真正的积木平台”

## 5. 长期演进阶段

### Phase 0: Freeze 现网真相

1. 冻结当前 active option strategy map
2. 冻结 `QQQ` stock baseline 与 option parity baseline
3. 明确 retired strategy list
4. 明确哪些策略允许继续 live tuning，哪些只保留回放价值

### Phase 1: 固化 Option Strategy Definition Contract

1. 输出统一 definition schema
2. 防止 plugin 自行覆盖逻辑核心字段
3. 将 module identity、policy identity 写入 audit
4. 让 config 只做实例化，不再承担逻辑路由

### Phase 2: 把平台核心积木做成一等模块

1. signal module
2. direction mapping module
3. contract policy module
4. execution policy module
5. direction mapping module
6. position / risk module
7. TP / trailing / breakeven module
8. flatten / recovery module

### Phase 3: 插件退化为 assembly adapter

插件最终只负责：

1. 指向 definition
2. 提供极少数实例级默认值
3. 提供 observability labels

插件不再成为“第二套策略逻辑容器”。

### Phase 4: 做跨策略通用性证明

至少要证明：

1. `QQQ` stock-signal option
2. `SPY` option-native MA
3. 第三个不同风格 option strategy

都能用同一个 kernel contract 接入。

当前状态：

1. 已完成
2. 当前覆盖样本：
   - `QQQ_INTRADAY_OPTION_PARITY_V1` -> `stock_signal_option`
   - `SPY_MA_CROSS_STACK_CONT_V2_DUAL` -> `option_native_ma`
   - `SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1` -> `generic_intraday_momentum`
3. 权威报告见 [OPTION_PLATFORM_COVERAGE_REPORT.md](./docs/guides/OPTION_PLATFORM_COVERAGE_REPORT.md)

### Phase 5: 做平台级 replay / shadow / acceptance gate

1. replay parity
2. shadow readiness
3. shadow acceptance
4. multi-day cutover recommendation
5. rollback drill

当前状态：

1. `QQQ` state shadow acceptance gate 已落地
2. option 平台级 admission gate 已落地，可统一汇总 inventory / assembly / coverage / active config contract
3. option strategy rollout evidence contract/report 已落地，能统一表达每个 family 的 replay / shadow / cutover 要求
4. 真正还没完成的是“每个 option family 的真实多日 replay / shadow 证据产出”

### Phase 6: 做交易员可配置化接入

目标不是“工程师会接”，而是：

1. 交易员知道自己在配什么
2. 文档能描述模块组合
3. 新策略主要通过 definition + config 完成
4. 策略上线前有统一 checklist

当前状态：

1. trader-facing schema 已落地
2. config example 已落地
3. onboarding checklist / risk matrix 已落地
4. 还没完成的是把这些 schema 校验接入 CI / release pipeline 强门槛

## 6. 非目标

以下事情不是这个主规划当前阶段的目标：

1. 为每一种 option alpha 写一个新内核
2. 为了快上线继续复制 legacy strategy class
3. 在没有 replay / shadow / acceptance 的情况下直接 live cutover
4. 为了“看起来更灵活”放松 ownership / protection / flatten authority

## 7. 相关文档

1. option 标准规范：[OPTION_PLUGIN_STRATEGY_STANDARD.md](./docs/guides/OPTION_PLUGIN_STRATEGY_STANDARD.md)
2. option 模块化计划：[OPTION_STRATEGY_MODULARIZATION_PLAN.md](./docs/OPTION_STRATEGY_MODULARIZATION_PLAN.md)
3. option active/retired inventory：[OPTION_STRATEGY_ACTIVE_RETIRED_INVENTORY.md](./docs/guides/OPTION_STRATEGY_ACTIVE_RETIRED_INVENTORY.md)
4. option assembly-adapter 合规报告：[OPTION_ASSEMBLY_ADAPTER_REPORT.md](./docs/guides/OPTION_ASSEMBLY_ADAPTER_REPORT.md)
5. option platform coverage 报告：[OPTION_PLATFORM_COVERAGE_REPORT.md](./docs/guides/OPTION_PLATFORM_COVERAGE_REPORT.md)
6. option platform admission gate：[OPTION_PLATFORM_ADMISSION_GATE.md](./docs/guides/OPTION_PLATFORM_ADMISSION_GATE.md)
7. option strategy rollout evidence：[OPTION_STRATEGY_ROLLOUT_EVIDENCE.md](./docs/guides/OPTION_STRATEGY_ROLLOUT_EVIDENCE.md)
8. option platform execution roadmap：[OPTION_PLATFORM_EXECUTION_ROADMAP.md](./docs/guides/OPTION_PLATFORM_EXECUTION_ROADMAP.md)
9. active strategy real-evidence status：[OPTION_ACTIVE_STRATEGY_EVIDENCE_STATUS.md](./docs/guides/OPTION_ACTIVE_STRATEGY_EVIDENCE_STATUS.md)
9. option trader-facing schema：[OPTION_TRADER_FACING_SCHEMA.md](./docs/guides/OPTION_TRADER_FACING_SCHEMA.md)
10. option onboarding checklist：[OPTION_STRATEGY_ONBOARDING_CHECKLIST.md](./docs/guides/OPTION_STRATEGY_ONBOARDING_CHECKLIST.md)
11. option risk matrix：[OPTION_STRATEGY_RISK_MATRIX.md](./docs/guides/OPTION_STRATEGY_RISK_MATRIX.md)
12. QQQ stock-signal option 设计：[QQQ_STOCK_SIGNAL_OPTION_STRATEGY_DESIGN.md](./docs/strategies/QQQ_STOCK_SIGNAL_OPTION_STRATEGY_DESIGN.md)
13. QQQ state machine 升级：[QQQ_STATE_MACHINE_UPGRADE_TODO_TEST_ACCEPTANCE.md](./docs/guides/QQQ_STATE_MACHINE_UPGRADE_TODO_TEST_ACCEPTANCE.md)
14. QQQ shadow acceptance pipeline：[QQQ_STATE_SHADOW_ACCEPTANCE_PIPELINE.md](./docs/guides/QQQ_STATE_SHADOW_ACCEPTANCE_PIPELINE.md)
