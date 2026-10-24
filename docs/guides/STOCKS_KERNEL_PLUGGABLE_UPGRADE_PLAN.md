# Stocks Kernel Pluggable Upgrade Plan

目的：把当前 `stocks_kernel` 从“可插拔的简单股票策略壳”升级成“可以表达任意股票/ETF/多腿执行策略逻辑的通用内核”。  
原则：`QQQ` 只是压力测试样本，不是架构目标。任何能力建设都必须先抽象成通用 contract，再用 `QQQ` 验证，不允许把 `QQQ` 特判直接写进 kernel。

## 0. 结论

当前方向是对的，但当前能力边界还不够。  
现在的 `stocks_kernel` 已经具备：

1. 配置驱动的 `feature/gate/policy/execution_profile`
2. 统一的 evaluator
3. lot / binding / audit / submit bridge 骨架
4. 可插拔的 policy 接口

但它仍然主要适用于：

1. 单一信号腿
2. 单一多空映射
3. 简单 entry
4. 简单 stop/tp/retreat
5. 单票单 lot 退出

如果目标是“复刻任意策略”，现在最需要做的不是继续堆策略细节，而是先升级 contract。

## 1. Review

### 1.1 现在已经正确的地方

1. `config_loader` 已经把配置组装和运行逻辑分开。
2. `evaluator` 已经把数据准备、feature 构建、gate 决策、signal 生成统一起来。
3. `execution kernel` 已经拥有 lot ledger、identity、fill attribution、binding book。
4. kernel 已经开始承担真正应该由内核保证的不变量：
   - broker truth 优先
   - pending 收敛
   - lot state machine
   - startup preflight
   - 审计链路

这些都是“可插拔架构”的正确方向，不应推倒。

### 1.2 当前最关键的结构性缺口

1. instrument model 过窄  
   当前更接近 `signal_symbol + long_exec_symbol + short_exec_symbol`。  
   这适合 `QQQ/TQQQ/SQQQ`、`SMH/SOXL/SOXS`，但不适合：
   - 同标的执行策略
   - pair / basket
   - 多执行腿
   - 保护腿独立路由

2. policy contract 过窄  
   当前 policy 主要表达：
   - `build_signal`
   - `build_entry_plan`
   - `build_stop_plan`
   - `build_take_profit_plan`
   - `build_retreat_plan`
   
   这不足以表达：
   - partial exit
   - reduce/add
   - privileged flatten
   - ensure protection
   - cancel working
   - recovery-time actions

3. exit / flatten semantics 不够一等  
   `flatten_action`、`close_only`、`reduce_only` 现在还不是真正的 kernel contract 核心字段。  
   复杂策略一定会依赖这些语义，如果继续放在零散 `extras` 里，后面会失控。

4. protection / recovery / session 仍然绑在 strategy shell  
   现在 `ComposableStocksKernelStrategy` 同时负责：
   - startup recovery
   - session gate
   - EOD flatten
   - protective OCO 载荷
   - runtime pending guard
   
   如果继续这样扩，最后会变成第二个巨石策略。

5. evaluator 结果模型还太简单  
   当前只产出一个 `entry_plan`。  
   复杂策略其实需要的是“按优先级排序的一组 action candidates”。

## 2. Critical

### 2.1 最危险的错误方向

1. 为了复刻 `QQQ`，把 `qqq_*` 特判直接写进 `stocks_kernel/strategy.py`
2. 继续增加策略专有 `extras`，但不提升 kernel contract
3. 让保护、flatten、recovery 继续依赖策略私有实现
4. 用 `QQQ` 单一样本验证，然后误以为内核已经通用

### 2.2 正确方向

1. 先补通用 action contract
2. 再拆模块边界
3. 再升级 execution lanes
4. 最后用 `QQQ` 和第二个风格不同的策略共同验收

一句话：

不是“为了复刻 `QQQ` 去改 kernel”，而是“为了让 kernel 可以表达复杂策略，用 `QQQ` 检查 contract 是否足够强”。

## 3. Todo List

### Phase 1: Contract 升级

目标：让 kernel 能表达复杂行为，而不是只表达一个 entry 和几个简化 exit。

1. 引入统一 `ActionPlan`
   字段至少包括：
   - `action_type`
   - `symbol`
   - `lane_id`
   - `qty`
   - `priority`
   - `position_lot_id`
   - `close_only`
   - `flatten_action`
   - `reduce_only`
   - `protection_attached`
   - `reason`
   - `metadata`

2. 重构 policy base
   改成返回 action list，而不是只返回单一 `entry_plan`

3. 重构 evaluator
   输出从单一 `entry_plan` 升级为：
   - `signal`
   - `action_candidates`
   - `winning_actions`
   - `blocked_actions`

4. 重构 submit bridge
   让 submit bridge 根据 `ActionPlan` 统一生成 envelope

### Phase 2: 模块边界升级

目标：把高层策略行为从 strategy shell 拆出去。

1. 抽出 `SessionModule`
   负责：
   - session window
   - no-new-after
   - flatten-before-close

2. 抽出 `ProtectionModule`
   负责：
   - protection ensure
   - stop / tp attachment
   - protection audit
   - protection self-heal

3. 抽出 `RecoveryModule`
   负责：
   - startup preflight
   - restart reconcile
   - stale runtime cleanup
   - recovery mode actions

4. 抽出 `ExecutionSemanticsModule`
   负责：
   - flatten priority
   - close_only / reduce_only enforcement
   - action dedupe / cooldown

### Phase 3: Instrument / Lane Model 升级

目标：从双腿映射升级成任意 lane/leg 表达能力。

1. 把 `KernelInstrumentBinding` 升级成更通用的 lane config
2. 支持：
   - `signal_lanes`
   - `execution_lanes`
   - `protection_lanes`
   - `hedge_lanes`
3. 每个 lane 要有：
   - symbol
   - side semantic
   - role
   - ownership scope
   - broker/exchange preferences

### Phase 4: QQQ 迁移验证

目标：不用特判，复刻复杂策略能力。

1. 先迁 `QQQ` 的通用能力，不迁 `QQQ` 名字
   - startup recovery gate
   - real-position insurance
   - flatten privileged path
   - protection audit/self-heal
   - TP1 / trailing / logic retreat ordering

2. 用 `QQQ` 作为第一个复杂样本验收

3. 再用第二个风格明显不同的策略验收
   例如：
   - 非代理 ETF 策略
   - 同标的执行策略
   - pair/basket

### Phase 5: 收口与模板化

目标：以后新策略接入主要是“写模块 + 配配置”。

1. 增加策略模板说明文档
2. 增加模块接入规范
3. 增加 production checklist
4. 增加 regression matrix

## 4. 测试方案

### 4.1 Contract 测试

1. `ActionPlan -> DecisionEnvelope` 语义不丢失
2. `close_only / flatten_action / reduce_only` 都能正确落地
3. `position_lot_id / order_ref / decision_id / trace_id` 串链完整

### 4.2 状态机测试

1. `intent_open -> pending_fill -> live -> pending_exit -> closed`
2. `accepted != filled`
3. partial fill 不会直接把 position 视为 fully live
4. exit fill 与 terminal update 幂等

### 4.3 Flatten / Protection 测试

1. flatten 优先级高于 entry/add/reduce
2. flatten action 会自动阻止后续 entry
3. live position 缺保护时会触发 `ensure_protection`
4. protection 缺失连续超时会 kill-switch flatten
5. flatten 后只清理相关保护单，不误伤自身 flatten 单

### 4.4 Recovery 测试

1. broker 有仓 / DB 无仓
2. DB 有活单 / broker 无单
3. stale pending entry
4. stale pending exit
5. protection 缺失恢复
6. startup gate 在 broker truth 未就绪时 fail-closed

### 4.5 Lane / Multi-Leg 测试

1. 单 signal -> 双 exec lane
2. 单 signal -> 单 exec lane
3. 多 lane flatten
4. foreign lane 不越权
5. lane-level audit detail 不串用

### 4.6 Anti-Specialization 测试

1. `QQQ` 风格复杂策略迁入后，不允许新增 `qqq_*` kernel 特判
2. 第二个非 `QQQ` 复杂策略可复用同一 contract
3. 若要支持新策略，只允许：
   - 增加 policy/module
   - 增加模板/配置
   - 增加 contract 兼容字段
   
   不允许直接改 kernel 主流程去迎合某一策略

## 5. 验收标准

### 5.1 架构层验收

1. kernel 主流程中不存在 `QQQ`、`SMH`、`SOXL`、`SOXS` 之类策略专名判断
2. `flatten_action`、`close_only`、`ensure_protection` 是 kernel contract 一部分
3. 保护、恢复、session 逻辑以模块形式挂接，而不是继续堆在一个 strategy shell 内

### 5.2 行为层验收

1. 能复刻 `QQQ` 股票策略的关键行为，不需要 kernel 特判
2. 能表达至少一种非 `QQQ` 的另一类复杂股票策略
3. broker / DB / runtime 三处最终一致
4. startup / restart / EOD / partial exit / protection recovery 都能稳定通过

### 5.3 研发流程验收

1. 新策略接入主要工作量在模块与配置，不在 kernel 主骨架
2. 每次新增能力时，都能归入：
   - contract 扩展
   - module 扩展
   - config 扩展
   
   而不是“某策略例外”

## 6. 实施顺序

1. 先做 `ActionPlan + submit_bridge + exit semantics`
2. 再做 `ProtectionModule + RecoveryModule`
3. 再做 `lane model`
4. 再做 `QQQ` 迁移验证
5. 最后做第二策略交叉验收

## 9. 当前落地进度（2026-04-02）

已完成：

1. `ActionPlan` contract 落地（types/policy/evaluator/submit_bridge 已接入，保持向后兼容）。
2. `submit_bridge` 已将 `close_only / flatten_action / reduce_only` 写入统一语义出口。
3. Phase 2 初始模块拆分完成，新增并接入：
   - `SessionModule`
   - `ProtectionModule`
   - `RecoveryModule`
4. Phase 3 初始 lane model 落地：
   - `KernelExecutionLane`
   - `KernelInstrumentBinding.execution_lanes`
   - `exec_symbols()` 与 `lane_for_side()` 能力
   - `config_loader` 可解析 `execution_lanes` 且兼容 legacy `long_exec_symbol/short_exec_symbol`
5. `ExecutionSemanticsModule` 已落地并接入主路径：
   - `flatten/exit` 候选过滤（防重复 flatten 提交）
   - active binding lot 的 entry 冲突去重语义
   - stale `pending_entry` 的超时 quarantine 判定
6. evaluator 已升级为多 `action_plans` 优先级主流程：
   - `EvaluationBundle.primary_action`
   - `on_features` 先按 `action_plans` 解析 primary entry，再应用 force-test 覆盖
   - 支持 policy 驱动 exit action（`FLATTEN/STOP/TAKE_PROFIT/RETREAT/REDUCE`）优先执行
7. lane/action 解析能力增强：
   - action 可通过 `lane_id` 路由到 execution lane symbol
   - action symbol 为 signal symbol 时自动映射到 side 对应 exec symbol
8. 兼容层完善：
   - `Policy.build_action_plans` 默认将 `build_exit_plans` 翻译为 action contract
   - 保持 `build_entry_plan`/`build_exit_plans` 旧接口兼容，不破坏既有策略

9. 模块边界进一步下沉完成（strategy 主壳瘦身）：
   - `SessionModule` 承接 execution/protection session extras
   - `ProtectionModule` 承接 offhours protective stop defaults
   - `RecoveryModule` 承接 broker/DB startup preflight 与 adapter/pg 解析
   - `ExecutionSemanticsModule` 承接 submit failure reason / kill-switch activation 语义
10. 端到端回放验收补齐：
   - 新增 PRE/RTH/POST/OVERNIGHT 全 session 下单语义回归测试
   - 新增非 `QQQ`（`IWM`）样本的内核通用性验收测试

未完成（后续持续项）：

1. 新增策略接入时持续扩展 regression matrix（作为平台常规工作，不再是本轮阻断项）。

## 7. 不该做的事

1. 不要先抄 `QQQ` 大段逻辑进 kernel
2. 不要继续在 `strategy.py` 里叠加更多策略行为
3. 不要把临时 `extras` 开关当长期 contract
4. 不要只用一个策略样本证明“架构可插拔”

## 8. Definition of Done

当下面 5 条同时成立，才算真正达到目标：

1. `QQQ` 可以通过模块化方式在 kernel 中实现
2. 第二种复杂策略也可以通过同一 contract 实现
3. kernel 主骨架没有策略专名分支
4. 风险与执行不变量由内核统一保证
5. 新策略开发主要是“配置 + 模块”，不是改内核
