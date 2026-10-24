# Option Strategy Todo / Test / Acceptance

目的：把 option 策略平台从“若干可运行插件”升级成“交易员可像搭积木一样组合策略”的正式落地清单。  
第一性原理：真正的 option 平台不是多几个策略类，而是让 signal、contract、execution、risk、flatten、ownership、acceptance 都成为标准模块与标准 contract。  
硬约束：`QQQ` 稳定路径先保住，任何平台化改造都不能先破坏现网稳定策略。

## 0. 模块地图

从专业期权交易员视角，一个可积木化的平台至少要支持 18 类模块：

1. underlying signal 模块
2. signal gating 模块
3. option direction mapping 模块
4. contract universe 模块
5. DTE policy 模块
6. delta / moneyness policy 模块
7. liquidity / spread / stale-quote gate 模块
8. entry execution 模块
9. close execution 模块
10. re-entry / cooldown 模块
11. sizing / capital reservation 模块
12. hard stop / logic retreat 模块
13. TP1 / TP2 / trailing / breakeven 模块
14. protection rebuild / self-heal 模块
15. flatten / panic / recovery 模块
16. order lifecycle / broker truth sync 模块
17. ownership / multi-strategy isolation 模块
18. audit / replay / shadow acceptance 模块

## 1. 当前最重要的缺口

### 1.1 策略表达层

1. definition contract 还不够强，plugin 仍可能携带逻辑私货。
2. signal 与 contract 之间的边界还没完全固定。
3. execution / risk / flatten 在不同策略家族中的表达还不够统一。
4. 同 underlying 多策略的 authority 规则仍需要更显式。

### 1.2 交易员接入层

1. 新策略仍偏工程师接入，不够“交易员配置化”。
2. 文档还没有把“哪些可以配、哪些不能配”固定成统一 schema。
3. 第二、第三种风格的 option 策略还没有充分证明平台通用性。

### 1.3 验收层

1. `QQQ` 的 shadow acceptance 骨架已经有了，但 option 平台整体准入门还没有统一成标准。
2. 新 option strategy 还缺少统一 replay / shadow / cutover checklist。
3. 缺少“新策略不符合 contract 时直接 block”的强门槛。

## 2. Todo List

### Phase A: 冻结现网与治理边界

1. 输出 active option strategy inventory
2. 输出 retired option strategy inventory
3. 冻结 `QQQ` stock baseline 与 option parity baseline
4. 冻结 live-tunable strategy list
5. 明确哪些 legacy 路径只允许 replay，不允许继续 live tuning

### Phase B: 固化 Strategy Definition Contract

1. 定义 option strategy definition schema
2. 强制字段：
   - `signal_module`
   - `direction_mapping_policy`
   - `contract_policy`
   - `execution_policy`
   - `position_policy`
   - `risk_policy`
   - `flatten_policy`
   - `ownership_scope`
   - `audit_namespace`
3. 定义 plugin 可覆盖字段白名单
4. audit 中写入 definition identity 与 module identity
5. config 层禁止承担核心逻辑路由

### Phase C: 抽象交易员真正关心的策略积木

1. 抽象 `UnderlyingSignalModule`
2. 抽象 `DirectionMappingPolicy`
3. 抽象 `OptionContractPolicy`
4. 抽象 `EntryExecutionPolicy`
5. 抽象 `CloseExecutionPolicy`
6. 抽象 `PositionSizingPolicy`
7. 抽象 `RiskStopPolicy`
8. 抽象 `ProfitManagementPolicy`
9. 抽象 `FlattenPolicy`
10. 抽象 `ProtectionRecoveryPolicy`

当前已落地的 first-class policy 基础模块：

1. `DirectionMappingPolicy`
2. `RiskPolicy`
3. `ProfitManagementPolicy`
4. `FlattenPolicy`
5. `StrategyDefinition` 持有上述模块 identity

### Phase D: 插件降级为装配器

1. plugin 只负责 definition 解析
2. plugin 只允许实例级默认值
3. plugin 不再包含第二套 signal / stop / flatten 逻辑
4. 新策略不得通过复制一个 legacy strategy class 上线

### Phase E: 做跨策略通用性证明

至少完成 3 条独立样本：

1. `QQQ` stock-signal option
2. `SPY` option-native strategy
3. 第三个不同形态 option strategy

证明内容：

1. 能共用同一 kernel
2. 能共用统一 definition contract
3. 能共用 replay / shadow acceptance gate
4. 多策略同 underlying 不串仓、不串单、不误 flatten

当前状态：

1. 已完成最小覆盖目标
2. 当前三条样本：
   - `qqq_intraday_option_v1_001` -> `stock_signal_option`
   - `spy_1dte_ma10_reject_0324_001` -> `option_native_ma`
   - `spy_intraday_momentum_option_paper_001` -> `generic_intraday_momentum`
3. 权威工具：
   - [build_option_platform_coverage_report.py](./tool/report/build_option_platform_coverage_report.py)
4. 当前阶段结论：
   - `distinct_archetype_count = 3`
   - `coverage_goal_met = true`
   - legacy 非 plugin-kernel 路径仍会出现在 report `error_count` 中，这是故意保留的治理信号，不是平台回退

### Phase F: 做平台级验收门

1. replay parity gate
2. state shadow readiness gate
3. state shadow acceptance gate
4. multi-day cutover recommendation
5. rollback drill
6. production block / allow checklist

当前状态：

1. `QQQ` state shadow readiness / acceptance / cutover recommendation 已落地
2. option platform admission gate 已落地，统一汇总：
   - inventory
   - assembly adapter compliance
   - platform coverage
   - active strategy config contract
3. option strategy rollout evidence contract/report 已落地，能统一检查：
   - replay harness
   - replay strategy id
   - parity profile
   - parity snapshot flag
   - cutover policy
4. 还没完成的是每个 option family 的真实 replay / shadow 证据产出
5. 权威文档：
   - [OPTION_PLATFORM_ADMISSION_GATE.md](./docs/guides/OPTION_PLATFORM_ADMISSION_GATE.md)
   - [OPTION_STRATEGY_ROLLOUT_EVIDENCE.md](./docs/guides/OPTION_STRATEGY_ROLLOUT_EVIDENCE.md)
   - [OPTION_PLATFORM_EXECUTION_ROADMAP.md](./docs/guides/OPTION_PLATFORM_EXECUTION_ROADMAP.md)
   - [OPTION_ACTIVE_STRATEGY_EVIDENCE_STATUS.md](./docs/guides/OPTION_ACTIVE_STRATEGY_EVIDENCE_STATUS.md)
   - [OPTION_LAUNCH_READINESS_DETAILED_PLAN.md](./docs/guides/OPTION_LAUNCH_READINESS_DETAILED_PLAN.md)
   - [OPTION_LAUNCH_EXECUTION_TODO.md](./docs/guides/OPTION_LAUNCH_EXECUTION_TODO.md)
   - [OPTION_LAUNCH_GO_NO_GO_GATE.md](./docs/guides/OPTION_LAUNCH_GO_NO_GO_GATE.md)
   - [QQQ_LAUNCH_GO_NO_GO.md](./docs/guides/QQQ_LAUNCH_GO_NO_GO.md)

### Phase G: 做交易员可配置化接入

1. 输出 trader-facing schema 说明
2. 输出配置模板
3. 输出模块选择指南
4. 输出新策略上线 checklist
5. 输出策略变更 risk matrix

当前状态：

1. trader-facing schema：已落地
2. definition-driven config template：已落地
3. onboarding checklist：已落地
4. risk matrix：已落地
5. 还没落地的是 CI / release 级强制校验

## 3. 测试计划

### 3.1 Definition / Contract 测试

1. definition schema 能表达完整 option strategy 组合
2. plugin 非白名单字段覆盖会被 block
3. config 改实例参数不会悄悄改逻辑模块
4. definition identity / module identity 会进入 audit

### 3.2 Signal / Mapping 测试

1. stock `LONG -> CALL`
2. stock `SHORT -> PUT`
3. disable-long / disable-short 规则正确
4. signal 被 gate 阻断时有明确 reason，而不是静默消失
5. 第二个策略可复用同一 signal module

### 3.3 Contract Selection 测试

1. preferred DTE 正确命中
2. preferred DTE 不可用时 fallback 正确
3. delta band 过滤正确
4. spread 守卫正确
5. stale quote 阻断正确
6. OI / liquidity 过滤正确

### 3.4 Execution 测试

1. accepted 不等于 filled
2. duplicate submit 不会重复开仓
3. timeout -> cancel -> cooldown 正确
4. degraded close 在 quote 坏时可安全退出
5. partial fill 不会误判 fully live
6. order lifecycle 与 broker truth 严格区分

### 3.5 Position / Risk 测试

1. sizing 不突破 capital limit
2. entry metadata 正确记录
3. underlying hard stop 正确触发
4. logic retreat 正确触发
5. EOD flatten cutoff 正确触发
6. panic flatten 优先级高于 normal exit

### 3.6 Protection / Flatten 测试

1. 开仓后 protection 存在
2. protection 缺失时可 rebuild
3. rebuild 失败时按规则 flatten 或 freeze
4. flatten lifecycle 能确认完成
5. manual / EOD / recovery / panic flatten 语义分离

### 3.7 Isolation / Ownership 测试

1. 同 underlying 多策略 ownership 正确
2. foreign order 不越权 cancel
3. foreign position 不越权 adopt
4. flatten authority 只作用于授权范围
5. protection authority 只作用于授权范围

### 3.8 Replay / Shadow / Acceptance 测试

1. legacy path vs modular path replay parity
2. shadow runtime transitions 完整
3. shadow order / flatten / protection transitions 完整
4. cross-state inconsistency 会被识别
5. acceptance runner 能给出 `PASS / WARN / FAIL`
6. projection-only evidence 不允许进入 cutover

### 3.9 Trader-Facing 配置测试

1. 新建同逻辑不同 underlying 的策略无需复制 signal code
2. 新建同 underlying 不同合约/风控策略无需改 kernel
3. 新增一个第三类 option strategy 时主要改 definition + config
4. 文档描述与系统行为一致

## 4. 验收标准清单

### 4.1 架构验收

1. 新 option 策略主要通过 definition + module + config 接入
2. kernel 主流程不含策略专名特判
3. plugin 不再承载第二套核心交易逻辑
4. 合约选择、execution、risk、flatten 都是显式模块

### 4.2 交易员体验验收

1. 交易员能明确知道自己在配置：
   - signal
   - contract
   - execution
   - risk
   - flatten
2. 同一逻辑换 underlying 主要是换 definition，不是复制策略类
3. 新策略上线前有统一 checklist，不靠口头约定

### 4.3 正确性验收

1. `QQQ` 稳定策略行为不被平台重构破坏
2. `QQQ` option parity 只在 replay / shadow / acceptance 通过后才允许 cutover
3. 至少 3 条不同风格 option strategy 证明平台通用性
4. 同 underlying 多策略并行不串仓、不串单、不误 flatten

当前状态：

1. 第 `3` 条已满足
2. 第 `1/2/4` 条还需要继续靠 replay / shadow / ownership authority 验证

### 4.4 可观测性验收

1. 每个策略实例都能看到 definition identity
2. 每次交易都能看到 module identity
3. replay / shadow / acceptance 能输出 trader-facing 结论
4. rollback 条件清晰可见

### 4.5 上线安全验收

1. 新策略必须先 replay
2. 新策略必须先 shadow
3. 没有真实 state 证据的日子不能算 cutover-grade evidence
4. projection-only evidence 只能是 `WARN`
5. multi-day acceptance 未通过前，不允许宣称“已完全落地可切换”

## 5. 现阶段最准确结论

从第一性原理看，当前代码已经满足：

1. option plugin kernel 与 strategy definition 骨架已存在
2. `QQQ` 的 state machine / shadow acceptance 骨架已存在
3. option 平台开始具备模块化和统一验收能力

但还没有自然满足：

1. 任意 option 策略都能像搭积木一样完全配置化接入
2. 插件完全退化为 assembly adapter
3. 至少 3 类策略已经证明通用性
4. 所有新 strategy 的统一准入门都已在生产实践中验证

所以当前阶段判断必须准确：

1. 不是“全部最终完成”
2. 是“方向、骨架、验收方法已经建立，后续应沿本清单继续推进”

## 6. 相关文档

1. 总规划：[OPTION_STRATEGY_FIRST_PRINCIPLES_MASTER_PLAN.md](./docs/guides/OPTION_STRATEGY_FIRST_PRINCIPLES_MASTER_PLAN.md)
2. option 标准规范：[OPTION_PLUGIN_STRATEGY_STANDARD.md](./docs/guides/OPTION_PLUGIN_STRATEGY_STANDARD.md)
3. option active/retired inventory：[OPTION_STRATEGY_ACTIVE_RETIRED_INVENTORY.md](./docs/guides/OPTION_STRATEGY_ACTIVE_RETIRED_INVENTORY.md)
4. option 模块化计划：[OPTION_STRATEGY_MODULARIZATION_PLAN.md](./docs/OPTION_STRATEGY_MODULARIZATION_PLAN.md)
5. QQQ stock-signal option 设计：[QQQ_STOCK_SIGNAL_OPTION_STRATEGY_DESIGN.md](./docs/strategies/QQQ_STOCK_SIGNAL_OPTION_STRATEGY_DESIGN.md)
