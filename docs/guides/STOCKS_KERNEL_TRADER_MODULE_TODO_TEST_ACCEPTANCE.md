# Stocks Kernel Trader Module Todo / Test / Acceptance

目的：从专业股票交易员如何拼装策略的视角，定义 `stocks_kernel` 需要提供的标准模块、升级 Todo、测试计划和验收标准。  
第一性原理：交易员不应该改 kernel 主骨架，而应该像搭积木一样组合模块，快速构造、验证和迭代股票交易策略。  
目标：让 `stocks_kernel` 不只是“能跑一个策略”，而是能稳定表达任意股票/ETF 日内策略的核心交易语义。

## 0. 模块地图

从专业交易员视角，一个模块化股票策略平台至少要支持 19 类模块：

1. 实时行情数据模块
2. 历史行情数据模块
3. 实时特征数据模块
4. 历史特征数据模块
5. 市场状态 / Session / Regime 模块
6. 标的定义 / Universe 模块
7. 信号逻辑模块
8. 门控逻辑模块
9. 仓位 sizing 模块
10. 入场执行模块
11. 持仓生命周期模块
12. 止损保护单模块
13. 止盈 / TP1 / Trailing 模块
14. 平仓 / Flatten 控制模块
15. 组合暴露 / 跨策略约束模块
16. 恢复 / 重启 / 审计回放模块
17. 动作仲裁 / 优先级裁决模块
18. 订单生命周期 / Broker 真相同步模块
19. 执行质量 / 滑点归因模块

## 1. 现阶段最重要的缺口

### 1.1 数据层

当前还不够好的点：

1. 实时数据是否足够可交易，还没有统一的 readiness contract。
2. 历史数据 continuity、gap、session boundary 还没有形成标准化判断。
3. feature readiness 还偏工程视角，不够交易语义化。
4. signal symbol 与 exec symbol 的数据 readiness 还没有被严格区分。

### 1.2 策略表达层

当前还不够好的点：

1. signal contract 还不够强，复杂策略仍难表达。
2. gate contract 还不够细，缺少 startup / real-position / open-order / degrade 等标准门控。
3. sizing 还没成为独立的一等模块。
4. strategy lifecycle 还不够标准化，很多行为仍散落在策略壳里。

### 1.3 执行层

当前还不够好的点：

1. entry submit / in-flight decision / trusted fill confirmation 仍未完全平台化。
2. protection 还没有形成完整的 `position-attached lifecycle`。
3. `TP1 -> trailing -> flatten` 的收益管理状态机还不完整。
4. flatten controller 与 recovery controller 还不够强。
5. 缺少统一的 action 冲突仲裁层。
6. 缺少订单生命周期与 broker truth 的专门平台层。
7. 缺少执行质量评估，无法判断 alpha 是否被执行劣化吃掉。

### 1.4 组合与运维层

当前还不够好的点：

1. 多策略同 symbol 的 ownership / flatten authority / protection authority 还需要继续标准化。
2. recovery / replay / audit 还缺少“交易员可直接复盘”的解释层。
3. regression matrix 仍偏开发视角，不够交易员验收视角。

## 2. Todo List

### 2.0 现网安全硬约束（先于所有功能）

1. 默认不改变现有策略路径：所有新能力必须 `feature flag` 默认关闭。
2. 严禁静默降级：动态注册配置错误必须 `fail-fast`，禁止“悄悄忽略继续跑”。
3. 必须可回滚：每一项改造都要支持仅通过配置切回 legacy 路径。
4. 渐进启用：仅允许按 strategy 粒度开启，不允许全局一次性切换。
5. 非 kernel / 旧策略零影响：未显式启用新路径的策略行为、默认参数、加载顺序必须保持等价。

### Phase A: 数据与市场状态模块

1. 增加统一 `MarketDataReadinessContract`
2. 区分 `signal_symbol_ready` 与 `exec_symbol_ready`
3. 增加 `HistoryReadinessContract`
4. 增加 `FeatureCriticalityContract`
5. 增加 `SessionState / RegimeState` 标准输出
6. 增加 `data_fail_policy`：
   - `fail_closed`
   - `degrade_open`
   - `flatten_only`

### Phase B: 策略表达模块

1. 将 signal contract 升级为：
   - `signal_context`
   - `action_candidates`
   - `priority`
   - `inhibition_reasons`
2. 将 gate contract 升级为：
   - `severity`
   - `recoverable`
   - `block_scope`
   - `retry_after_sec`
3. 将 sizing 升级为独立模块：
   - fixed dollar
   - fixed shares
   - ATR risk
   - volatility-adjusted
   - conviction-based
4. 增加 `UniverseDefinition` 与 `InstrumentDefinition`

### Phase C: 执行模块

1. 增加统一 `DecisionLock`
2. 增加 trusted fill confirmation contract
3. 增加 submit timeout -> cancel -> cooldown 主路径
4. 增加入场执行策略模块：
   - market
   - limit
   - breakout
   - pullback
   - scale-in
   - re-entry cooldown
5. 增加 `PositionLifecycle` 状态：
   - pending_fill
   - live
   - post_tp1
   - pending_exit
   - closed
   - quarantined
6. 增加 `OrderLifecycle / BrokerTruthSync`
7. 增加 `ActionArbiter / ExecutionPriorityResolver`

### Phase D: 保护与收益管理模块

1. 将 protection 升级为完整模块：
   - entry attach
   - restart rebuild
   - missing stop self-heal
   - protection audit
2. 增加标准化 `TP1` partial exit contract
3. 增加标准化 `TrailingStop` contract
4. 增加 `BreakevenShield` contract
5. 增加 `DynamicStopAdjust` contract
6. 增加 `unsafe_position_without_protection -> flatten` 规则

### Phase E: 平仓与恢复模块

1. 增加 `FlattenController`
2. 区分：
   - EOD flatten
   - panic flatten
   - recovery flatten
   - manual flatten
3. 增加 flatten completion confirmation
4. 增加 restart recovery policy
5. 增加 startup degraded-open / hard-block 模板

### Phase F: 组合与运维模块

1. 增加 `PortfolioExposurePolicy`
2. 增加多策略 ownership contract
3. 增加 replay / explainability 层
4. 增加 trader-facing audit schema
5. 增加跨策略 regression matrix
6. 增加 `ExecutionQuality / SlippageAttribution`

## 3. 测试计划

### 3.0 安全回归先行（阻断门）

1. `stocks_kernel` 新增 flag 全关时，历史行为快照与整改前等价（决策数、submit 数、flatten 行为一致）。
2. 非 `stocks_kernel` 策略冒烟回归必须通过（尤其当前在跑的老策略）。
3. 动态注册错误配置必须在 `initialize` 阶段直接失败，并输出明确报错字段。
4. rollback 演练：仅修改配置将新路径关闭后，策略可恢复 legacy 运行。

### 3.1 数据模块测试

1. 实时 signal 数据 stale，entry blocked
2. exec symbol snapshot missing，entry blocked 或 degrade
3. 历史 bars 不足，warmup fail-closed
4. session boundary gap，continuity blocked
5. offhours / overnight 数据在不同 profile 下行为正确

### 3.2 市场状态 / Session / Regime 测试

1. 开盘窗口策略只在允许窗口交易
2. no-new-after 生效
3. flatten-before-close 生效
4. regime 切换时 action set 正确变化
5. degrade-open / hard-block 行为符合配置

### 3.3 信号与门控测试

1. signal 正常生成 action candidates
2. gate 阻断时 action 被标记 blocked 而不是静默消失
3. startup gate 阻断 entry
4. real-position insurance gate 阻断 entry
5. open-order hard block gate 阻断 entry
6. second strategy 可复用同一 signal/gate contract

### 3.4 Sizing 测试

1. fixed dollar sizing 正确
2. ATR risk sizing 正确
3. volatility-adjusted sizing 正确
4. sizing 不突破 capital_limit_usd
5. sizing 与 reservation / protection qty 一致

### 3.5 入场执行测试

1. accepted 不等于 filled
2. duplicate submit 不会重复开仓
3. timeout 后 cancel + cooldown 生效
4. re-entry cooldown 生效
5. partial fill 不会直接当 fully live
6. broker ack / submit / fill / terminal 状态严格区分
7. orphan / ghost / mismatch 出现时 freeze 语义正确

### 3.6 持仓生命周期测试

1. `pending_fill -> live -> post_tp1 -> pending_exit -> closed`
2. fill 与 terminal update 幂等
3. quarantine state 不允许继续 normal entry
4. restart 后 lifecycle 能恢复到正确状态

### 3.7 止损保护单测试

1. entry 后保护单存在
2. restart 后保护单缺失可 rebuild
3. rebuild 条件不足则 flatten
4. protection audit 检测 missing stop
5. self-heal 节流与去重正确

### 3.8 止盈 / Trailing 测试

1. TP1 partial exit 后剩余仓位正确
2. trailing anchor / floor 更新正确
3. trailing stop 触发时只平剩余仓位
4. breakeven shield 方向与价格正确
5. dynamic stop adjust 不破坏 hard stop invariant

### 3.9 平仓测试

1. flatten 优先级高于 entry/add/reduce
2. flatten 不误伤自身订单
3. flatten 后 broker/DB/runtime 全 flat
4. EOD flatten 与 panic flatten 语义分离
5. quote 缺失时 flatten fallback 正确

### 3.10 组合与恢复测试

1. 同 symbol 多策略 ownership 正确
2. foreign position / foreign order 不越权
3. broker 有仓 / DB 无仓 恢复正确
4. DB 有单 / broker 无单 恢复正确
5. replay 可还原一笔交易全链路
6. action 冲突时优先级裁决结果稳定一致
7. execution quality 指标可产出并可归因

## 4. 验收标准清单

### 4.1 模块化验收

1. 新策略主要通过：
   - 配置
   - signal module
   - gate module
   - sizing module
   - execution/protection/flatten module
   完成接入，而不是改 kernel 主骨架
2. kernel 主流程不包含策略专名特判
3. 第二种非 `QQQ` 风格策略能接入同一 contract

### 4.2 交易员视角验收

1. 交易员能独立配置：
   - 交易时间
   - 信号
   - gate
   - sizing
   - stop
   - TP1/trailing
   - flatten
2. 交易员可以明确知道：
   - 为什么进
   - 为什么没进
   - 为什么减仓
   - 为什么平仓
3. 交易员可以在不改 kernel 的前提下拼装出新策略

### 4.3 执行正确性验收

1. `accepted != filled` 全链路成立
2. live position 永远满足 protection invariant
3. flatten 绝对高于 entry/add/收益优化
4. broker / DB / runtime 最终一致
5. ghost/orphan/mismatch 不会在脏状态下继续交易
6. 动作冲突一定经过统一仲裁，而不是多个模块各自下命令

### 4.4 风险验收

1. 无保护仓位不会继续正常交易
2. stale pending entry 不会无限悬挂
3. terminal orphan / ghost order 触发 freeze 或收敛
4. sizing / reservation / protection qty 永远一致
5. protection 自愈超过阈值后必须升级为 flatten 或 freeze

### 4.5 验证覆盖验收

1. 覆盖 PRE / RTH / POST / OVERNIGHT
2. 覆盖 startup / restart / EOD / panic / flatten
3. 覆盖 stock / ETF / 同标的执行 / 双腿执行基础场景
4. regression matrix 持续可扩展

### 4.6 Wave 1 硬阈值验收

1. `ENTRY ack -> trusted fill / trusted terminal` 必须在配置的 timeout 窗口内收敛，默认验收阈值 `<= 60s`。
2. stale pending entry 到 quarantine/cancel/cooldown 的总收敛时间，默认验收阈值 `<= 75s`。
3. live position 缺 protection 时，默认验收阈值：
   - `<= 10s` 检测到 missing protection
   - `<= 30s` 完成 self-heal 或升级为 flatten/freeze
4. ghost/orphan/mismatch 出现后，相关 lane 的新 entry 必须在下一次 action 评估周期内被冻结。
5. sizing 与 reservation / protection qty 的偏差验收阈值为 `0`，不允许容差漂移。
6. flatten 发出后，broker/DB/runtime 达到 flat 的默认验收阈值 `<= 90s`，否则必须升级为高优先级异常。

### 4.7 不影响其他策略验收（强制）

1. 未启用 `stocks_kernel` 新 flag 的策略，运行结果与整改前保持等价。
2. `options`、`qqq_intraday`、其他 legacy 策略的默认配置不新增必填项、不改变默认值语义。
3. 配置缺陷不会“部分生效”：动态策略/门控/stage 注册配置错误必须启动失败，不允许隐式跳过。
4. 任一整改项都存在明确回滚开关，且回滚后可在同一交易日恢复可运行状态。

## 5. 一句话定义 Done

当一个专业股票交易员可以在不改 `stocks_kernel` 主骨架的情况下，仅通过“模块 + 配置”组合出：

1. 可解释的 signal
2. 稳定的 gate
3. 正确的 sizing
4. 安全的 entry
5. 完整的 protection
6. 分层的 TP / trailing / flatten
7. 可恢复、可审计、可回放的执行链

就说明这个平台真正具备了可插拔能力。

## 6. 本轮落地清单（2026-04-10）

### 6.1 P0 立即落地

1. `policy_registry_specs` 增加独立开关：`enable_dynamic_policy_registry=true` 才允许动态注册。
2. `PolicyRegistry.register_from_specs` 改为 fail-fast（字段缺失/导入失败/attr 缺失/目标不可调用直接报错）。
3. 补单测覆盖：
   - 未开 `enable_dynamic_policy_registry` 时拒绝动态 policy specs。
   - `policy_registry_specs` 非法配置时报错，不再静默忽略。

### 6.2 P1 紧随落地

1. 给动态 stage/gate/policy 统一加“启动审计摘要”（注册项数、成功项、失败项、flag 状态）。✅ 已落地（`dynamic_registry_startup_summary`）。
2. 加 shadow parity 检查脚本，比较 legacy/new contract 输出差异但不影响交易决策。
3. 补跨策略回归矩阵（至少覆盖 stocks/options/qqq_intraday 的启动与 1 分钟运行冒烟）。
