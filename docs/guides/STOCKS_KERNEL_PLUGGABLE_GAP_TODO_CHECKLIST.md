# Stocks Kernel Pluggable Gap / Todo / Acceptance Checklist

目的：把当前 `stocks_kernel` 与 `qqq_intraday_strategy` 的关键能力差距，按链路拆成可执行的升级清单。  
原则：目标不是“把 QQQ 写死进 kernel”，而是让 kernel 具备复刻复杂策略的通用表达能力。  
结论：当前 `stocks_kernel` 已经有了正确的骨架，但在“复杂策略的一致性”和“生产级执行完整性”上仍有明显缺口，尤其在数据纪律、保护单闭环、退出状态机、恢复语义与验收矩阵上。

## 0. 总体结论

当前还不够好的地方，不是单点 bug，而是 5 类结构性不足：

1. 数据链路还没有形成统一、强约束的 freshness / continuity / readiness discipline。
2. 信号与门控 contract 还偏简化，无法完整表达复杂策略在不同 session / recovery / warmup 阶段的行为。
3. 下单与成交确认虽然已经比以前安全，但还没有完全达到 `QQQ` 那种“accepted != filled + in-flight lock + trusted confirmation”强度。
4. 保护单、Trailing Stop、TP1、Breakeven、Restart Protection 还没有形成完整 position-attached 生命周期。
5. 测试与验收仍偏 unit/regression，缺少跨 session、跨状态、跨策略类型的统一验收矩阵。

## 1. 分链路 Gap Review

### 1.1 实时行情数据

当前不够好：

1. 对实时行情的 freshness 约束还偏弱，更多停留在 feature/gate 层，没有形成统一 market-data contract。
2. 对 `quote missing / stale quote / snapshot missing / exec symbol snapshot missing` 的处理存在，但不够系统化。
3. 不同 session 的数据可用性语义还不够强，例如：
   - `PRE/POST/OVERNIGHT` 是否允许无 bid/ask 退化
   - 不同交易所下 snapshot 合法性要求
   - signal symbol 与 exec symbol 数据是否必须同时就绪
4. 还没有统一定义“允许交易的数据最低完备条件”。

升级 Todo：

1. 引入统一 `MarketDataReadinessContract`：
   - `last_trade_ready`
   - `nbbo_ready`
   - `exec_symbol_ready`
   - `signal_symbol_ready`
   - `session_supported`
   - `freshness_ok`
   - `continuity_ok`
2. 为 signal symbol / exec symbol / protection symbol 分别定义 readiness。
3. 明确每种 action 对数据的最低要求：
   - `ENTRY` 需要什么
   - `STOP` 需要什么
   - `TP1/TRAILING` 需要什么
   - `FLATTEN` 在 quote 缺失时的 fallback 是什么
4. 统一处理 offhours/overnight 数据降级策略，避免每个策略自己判断。

### 1.2 历史行情数据

当前不够好：

1. 历史数据加载虽然统一到了 evaluator，但仍偏“够用就行”，缺少 continuity / gap / session boundary 语义。
2. 没有统一定义：
   - 开盘前 warmup 最少需要多少 bars
   - 隔夜切换后历史窗口如何重建
   - 历史缺口时是 fail-closed 还是 degrade-open
3. 对 startup/restart 后第一段历史数据的可信度没有统一 contract。

升级 Todo：

1. 引入统一 `HistoryReadinessContract`：
   - `min_bars_ready`
   - `session_boundary_ok`
   - `gap_count`
   - `continuity_ok`
   - `source_priority`
2. 将 warmup contract 从策略私有行为升级成 kernel contract。
3. 增加 restart 后历史窗口重建逻辑，避免“冷启动历史不足但直接进入正常逻辑”。
4. 明确不同策略模板对历史窗口的声明方式，而不是散落在 `extras`。

### 1.3 实时特征数据

当前不够好：

1. feature registry 已统一，但 feature readiness 仍偏“字段存在/不存在”，缺少交易级语义。
2. 没有统一定义哪些 feature 是：
   - entry-critical
   - exit-critical
   - trailing-critical
   - protection-rebuild-critical
3. 特征 stale、缺失、跨 session 失真时，缺少统一 degrade policy。

升级 Todo：

1. 增加 feature criticality contract：
   - `entry_required`
   - `exit_required`
   - `optional_with_degrade`
2. 对每个 feature 明确 freshness / max_age / continuity 要求。
3. evaluator 输出里补齐：
   - `critical_features_missing`
   - `degraded_features`
   - `action_block_reasons`
4. 禁止策略直接凭原始 features 判断“还能不能交易”，必须通过 kernel readiness 输出。

### 1.4 历史特征数据

当前不够好：

1. 还没有统一管理“历史特征”与“历史行情”的依赖边界。
2. 类似 seed、anchor、opening-window、continuity 派生特征，当前仍更像策略行为，不是通用 contract。
3. 缺少 feature replay / feature parity 测试，难以保证重启前后特征一致。

升级 Todo：

1. 把 seed/anchor/opening-window 这种“历史衍生上下文”抽象成通用 feature module。
2. 为历史特征定义持久化/重建/回放 contract。
3. 增加 feature parity regression：
   - 同一输入历史 -> 同一特征输出
   - restart 前后输出一致
   - session 切换边界不漂移

### 1.5 信号逻辑

当前不够好：

1. `QQQ` 的复杂 entry model、intraday regime、opening logic、re-entry logic 还没有被提升成通用 signal contract。
2. 目前 policy 更接近“单一方向信号 + 单笔 entry plan”，还无法完整表达：
   - 分阶段 entry
   - add/reduce
   - TP1/trailing 路径
   - recovery-time corrective action
3. 现在 `stocks_kernel` 里的 `QQQIntradayPolicyV1` 本质是 QQQ 的简化版，不是 parity 版。

升级 Todo：

1. 将 signal contract 升级为：
   - `signal_context`
   - `action_candidates`
   - `priority`
   - `inhibition_reasons`
2. policy 必须能表达：
   - `ENTRY`
   - `ADD`
   - `REDUCE`
   - `TP1`
   - `TRAILING_STOP`
   - `ENSURE_PROTECTION`
   - `FLATTEN`
3. 建立“signal model 与 execution semantics 解耦”规范：
   - 策略只提 action
   - kernel 决定执行优先级和不变量

### 1.6 门控逻辑

当前不够好：

1. gate pipeline 已存在，但与 `QQQ` 的稳定性/完整性/启动期 gate 仍不一致。
2. `QQQ` 有更强的 gate：
   - seed readiness
   - startup anchor embargo
   - restart degrade
   - real-position insurance
   - open-order hard block
3. 当前 kernel 仍缺少统一 gate ownership 和 gate priority 定义。

升级 Todo：

1. 将下列 gate 提升为 kernel 标准 gate：
   - `startup_recovery_gate`
   - `seed_readiness_gate`
   - `data_integrity_gate`
   - `real_position_insurance_gate`
   - `open_order_hard_block_gate`
   - `session_transition_gate`
2. 每个 gate 必须明确：
   - fail-open / fail-closed
   - audit payload
   - recovery path
3. gate 不再只返回 allowed/blocked，要返回：
   - `severity`
   - `recoverable`
   - `retry_after_sec`
   - `block_scope`

### 1.7 下单

当前不够好：

1. `accepted != filled` 已经在系统层有进步，但还没完全达到 `QQQ` 那种“trusted confirmation only”强度。
2. 还缺少完整的 in-flight decision lock 语义：
   - pending decision 生命周期
   - submit 后 grace period
   - timeout cancel / timeout cooldown
3. 部分 dedupe 存在，但没有完全统一成 kernel contract。

升级 Todo：

1. 引入统一 `DecisionLock` / `InFlightEntryState`：
   - `decision_id`
   - `set_at`
   - `grace_until`
   - `timeout_sec`
   - `cooldown_until`
2. fill/terminal update 只能通过 trusted identity 解锁。
3. 增加 submit-timeout 自动收敛路径：
   - cancel live order
   - clear pending
   - arm cooldown
4. 对 `ENTRY/ADD/EXIT/FLATTEN` 分别定义 submit success / live / terminal 语义。

### 1.8 止损保护单

当前不够好：

1. kernel 已能在 entry 时带 `protective_oco`，但还不等价于 `QQQ` 的持续保护闭环。
2. 当前缺少：
   - live protection ensure loop
   - protection audit loop
   - missing protection self-heal
   - restart 后 stop rebuild / unprotected flatten
3. protection 仍然更像“下单时附带的 payload”，不是 position-attached lifecycle。

升级 Todo：

1. 将 `ProtectionModule` 升级成完整的 position-attached manager：
   - entry attach
   - restart rebuild
   - missing stop self-heal
   - missing context fallback flatten
2. 引入保护单 invariant：
   - `live long/short position -> active broker protection required`
3. 增加 protection audit cadence 与 throttle。
4. 对 stop rebuild 明确要求：
   - 何时允许 rebuild
   - 何时必须 flatten
   - 缺 entry_px / ATR / qty 时的行为

### 1.9 止盈单 / Trailing Stop 动态止盈单

当前不够好：

1. 这是现在与 `QQQ` 差距最大的部分之一。
2. 当前 kernel 只有简化的 `TAKE_PROFIT` 与 `RETREAT`，没有：
   - TP1 partial exit
   - trailing high / floor
   - breakeven shield
   - post-TP1 flatten path
3. 当前退出状态机不足以表达“先减仓，再移动保护，再跟踪退出”的链路。

升级 Todo：

1. 将 `TP1 / trailing / breakeven / dynamic stop widen` 升级为标准 action semantics。
2. 为 lot 增加标准化 runtime fields：
   - `tp1_hit`
   - `trail_anchor_px`
   - `trail_floor_px`
   - `breakeven_armed`
   - `dynamic_stop_state`
3. 支持 partial exit 后的剩余仓位管理。
4. 明确 trailing stop 与 hard stop、logic retreat、flatten 的优先级。

### 1.10 平仓逻辑

当前不够好：

1. flatten 已有独立语义，但和 `QQQ` 的 privileged flatten path 还不完全一致。
2. 当前 kernel 还缺少：
   - flatten 前 working order cleanup policy
   - flatten 后强确认路径
   - EOD flatten 与 recovery flatten 的区别
   - flat 后保护单/残留单收口纪律
3. flatten 的 fallback 行为与 quote 缺失场景还不够完整。

升级 Todo：

1. 引入统一 `FlattenController` 级别语义到 stocks kernel：
   - `why flatten`
   - `scope`
   - `cancel_before_flatten`
   - `quote_fallback_policy`
   - `completion_confirmation`
2. 平仓后必须重新核对：
   - broker qty == 0
   - DB qty == 0
   - protection orders == 0
   - reservations == 0
3. `EOD flatten`、`panic flatten`、`restart flatten` 必须分开建模。

## 2. 升级 Todo List

### Phase A: 数据与特征 Contract

1. 建立 `MarketDataReadinessContract`
2. 建立 `HistoryReadinessContract`
3. 建立 `FeatureCriticalityContract`
4. evaluator 输出 `action_candidates / blocked_actions / degraded_reasons`
5. 去掉策略私有的 readiness 判断，统一走 kernel contract

### Phase B: Signal / Gate 升级

1. 增加 `seed_readiness_gate`
2. 增加 `startup_recovery_gate`
3. 增加 `real_position_insurance_gate`
4. 增加 `open_order_hard_block_gate`
5. policy 支持 `ENTRY/ADD/REDUCE/TP1/TRAILING/ENSURE_PROTECTION/FLATTEN`

### Phase C: Submit / Fill / Decision Lock

1. 引入统一 `DecisionLock`
2. submit timeout -> cancel -> cooldown 主路径
3. trusted fill confirmation contract
4. terminal update 幂等清锁
5. accepted / submitted / filled / live / terminal 状态链标准化

### Phase D: Protection / Trailing / Exit State Machine

1. 将 protection 升级为 position-attached lifecycle
2. 新增 protection audit loop 与 self-heal
3. 新增 TP1 partial exit contract
4. 新增 trailing stop contract
5. 新增 breakeven shield / dynamic stop widen contract
6. 新增 restart protection rebuild / unsafe -> flatten contract

### Phase E: Flatten / Recovery / Lane

1. flatten controller 语义标准化
2. startup gate / degrade-open / hard-block 策略模板化
3. lane ownership / protection lane / hedge lane 扩展
4. multi-lot / partial-exit / same-symbol same-side 多 lot 管理
5. second strategy cross-validation

## 3. 测试计划

### 3.1 实时行情数据测试

1. signal symbol fresh, exec symbol stale -> entry blocked
2. signal symbol stale, exec symbol fresh -> entry blocked
3. offhours quote 缺 bid/ask -> 按配置降级或阻断
4. overnight exchange override 正确落地
5. exec snapshot 等待窗口超时后行为正确

### 3.2 历史行情数据测试

1. warmup bars 不足 -> fail-closed
2. session boundary gap -> 触发 continuity block
3. restart 后 history rebuild 成功
4. 历史缺口修复前不允许进入正常 entry

### 3.3 实时/历史特征测试

1. entry-critical feature 缺失 -> entry blocked
2. exit-critical feature 缺失 -> 允许 flatten，不允许复杂收益管理
3. same input history -> same derived features
4. restart 前后 feature parity

### 3.4 信号与门控测试

1. seed not ready -> blocked
2. startup embargo -> blocked
3. restart degrade active -> blocked
4. open orders present -> blocked
5. real broker position present -> blocked
6. second strategy, same contract -> 不需要 kernel 特判

### 3.5 下单测试

1. `accepted != filled`
2. duplicate submit 不生成第二张 live order
3. timeout 后自动 cancel + cooldown
4. terminal update 幂等
5. fill attribution 串到正确 lot / decision / order_ref

### 3.6 保护单测试

1. entry 后保护单存在
2. restart 后保护单缺失 -> rebuild
3. rebuild 条件不足 -> flatten
4. protection audit 连续 missing -> self-heal / kill-switch
5. flatten 后保护单正确撤销

### 3.7 止盈 / Trailing 测试

1. TP1 partial exit 后剩余仓位正确
2. trailing anchor / floor 更新正确
3. trailing stop 触发后只平剩余仓位
4. breakeven shield 不会把 stop 挂到错误方向
5. dynamic widen 不会覆盖 hard stop invariant

### 3.8 平仓测试

1. EOD flatten 优先于 entry/add
2. flatten 前 cleanup 不误伤 flatten 单
3. flatten 完成后 broker/DB/runtime 全 flat
4. quote 缺失时 flatten fallback 生效
5. panic flatten / restart flatten / eod flatten 语义分离

### 3.9 Recovery 测试

1. broker 有仓 / DB 无仓
2. DB 有仓 / broker 无仓
3. pending entry 残留
4. pending exit 残留
5. ghost order / orphan order / mismatch 修复
6. protection context 缺失恢复

### 3.10 Anti-Specialization 测试

1. `QQQ` parity 迁入后 kernel 不允许新增 `qqq_*` 特判
2. `IWM` 或同标的非杠杆 ETF 策略可复用同一 contract
3. 第二种风格明显不同的策略也能接入：
   - 同标的执行
   - 双腿执行
   - partial exit

## 4. 验收标准清单

### 4.1 数据层验收

1. 所有交易动作都能声明最低数据要求
2. 数据缺失/过期时行为可解释、可审计
3. session 切换不引发 silent degrade

### 4.2 策略层验收

1. signal/gate/action contract 能表达 `QQQ` 复杂行为
2. 同一 contract 能表达至少一个非 `QQQ` 复杂策略
3. 不需要 kernel 特判策略专名

### 4.3 执行层验收

1. `accepted != filled` 全链路成立
2. duplicate submit 不会重复开仓
3. pending/terminal/fill 状态收敛明确
4. trusted fill / trusted terminal update 语义成立

### 4.4 风控层验收

1. live position 永远有 protection invariant
2. protection 缺失时系统进入 self-heal 或 flatten，不继续正常交易
3. flatten 绝对高于 entry / add / reduce / trailing 优化

### 4.5 一致性验收

1. broker / DB / runtime 最终一致
2. restart / startup / EOD / offhours / overnight 都能通过
3. ghost/orphan/mismatch 处理后不会留下脏状态继续交易

### 4.6 工程验收

1. 新策略主要通过：
   - 配置
   - policy
   - module
   完成接入，而不是改 kernel 主骨架
2. regression matrix 覆盖：
   - session
   - data readiness
   - entry/exit/protection/flatten/recovery
   - stock / ETF / option-compatible semantics

## 5. 一句话结论

当前 `stocks_kernel` 已经具备“做成可插拔平台”的骨架，但距离“能无特判复刻 QQQ 且复刻别的复杂策略”还差 4 个核心升级：

1. 数据 readiness contract
2. 强 gate + decision lock contract
3. position-attached protection / TP1 / trailing / flatten 状态机
4. 覆盖多 session / 多策略类型的统一验收矩阵

只有这 4 件事补齐，`stocks_kernel` 才算真正具备长期可插拔能力。
