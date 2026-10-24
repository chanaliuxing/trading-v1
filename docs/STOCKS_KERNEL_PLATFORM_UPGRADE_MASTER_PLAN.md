# Stocks Kernel Platform Upgrade Master Plan

## 1. 目标定义

本计划的唯一目标不是“再做一个更好的 index_super_win”。

唯一目标是交付一个**任意股票策略可直接生产可用的通用框架**。

该框架必须满足：

1. 新增股票策略时，不重写执行、持仓、对账、残留清理、IBKR 同步主链
2. 交易员主要只配置：
   - data profile
   - feature set
   - gate pipeline
   - policy template
   - policy params
   - universe
3. 现有稳定策略，尤其 `qqq_intraday_001`，在平台未完成前不受影响
4. 平台未来可支持：
   - 多 symbol 并行
   - 多策略并行
   - 同 symbol 多策略并行
5. 出问题时必须能定位、隔离、恢复、收敛

## 2. 非目标

在平台完成前，以下不是当前目标：

1. 直接替换现有 `qqq_intraday_001`
2. 让旧 `qqq_intraday_001` 和新 `stocks_kernel` 在同一账户同票 live 并行
3. 把 `stocks_kernel` 当前状态宣传为“已经生产可用”

## 3. 硬约束

### 3.1 现网保护

1. `qqq_intraday_001` 继续作为生产稳定策略
2. 平台升级不允许改 `qqq_intraday_001` 的交易逻辑来适配新框架
3. 新框架开发期间，`QQQ/TQQQ/SQQQ` 不允许作为 live 实验标的
4. 平台未通过验收前，`stocks_kernel` 只能 shadow/replay/独立 symbol paper

### 3.2 生产禁令

在旧 `qqq_intraday_001` 仍是生产策略时，必须禁止：

1. `stocks_kernel` 同票 live
2. policy 直接访问 broker / DB / runtime internals
3. gate 直接提交订单
4. strategy shell 承载具体交易逻辑
5. 新策略实例改 execution / reconcile / cleanup 主链

### 3.3 真相源优先级

平台必须固定真相源顺序：

1. broker execution / broker position / broker open order
2. DB order / fill / position ledger
3. position_lot ledger
4. runtime snapshot
5. strategy local memory

任何冲突恢复都必须按此顺序收敛。

## 4. 产品分层

平台必须拆成两个产品，而不是一个大策略类。

### 4.1 Product A: Platform Core

职责：

1. execution kernel
2. order identity
3. position lot ownership
4. fill attribution
5. stop / flatten / reconcile
6. broker sync
7. residual cleanup
8. observability / audit

### 4.2 Product B: Strategy Builder

职责：

1. data profile
2. feature plugin
3. gate plugin
4. policy template
5. strategy instance config

Builder 必须让交易员最终只通过：

1. 配置
2. 模板
3. 参数

就能生成一个新股票策略实例。

## 5. 目标架构

### 5.1 Data Layer

必须交付：

1. `RealtimeFeatureProvider`
2. `HistoryProvider`
3. `BackfillProvider`
4. `SessionResolver`
5. `ReadinessEvaluator`

要求：

1. 策略层不关心数据来自 Kafka / IBKR / ClickHouse
2. 统一输出 `MarketSnapshot + HistoryBundle`
3. RTH / premarket / afterhours / overnight 由 profile 决定，不写死在策略里

### 5.2 Feature Layer

必须交付：

1. `FeaturePlugin`
2. `FeatureRegistry`
3. `FeatureDependencyGraph`
4. `FeatureCache`

要求：

1. 新增 feature 不改主 runtime
2. feature 可以声明依赖与 warmup 条件
3. feature readiness 必须统一可审计

### 5.3 Gate Layer

必须交付：

1. gate registry
2. gate config schema
3. gate reason code 标准
4. gate pipeline runner

要求：

1. 新增 gate 只加插件和配置
2. 不允许继续使用大量 `if/elif` gate 工厂作为最终形态
3. gate 输出必须带 trader reason code

### 5.4 Policy Layer

必须交付完整 policy contract：

1. `build_signal`
2. `build_entry_plan`
3. `build_add_plan`
4. `build_stop_plan`
5. `build_take_profit_plan`
6. `build_exit_plans`
7. `build_retreat_plan`
8. `on_fill_feedback`

要求：

1. policy 只定义 alpha / trade management
2. policy 不直接触达 broker / DB / runtime state
3. 新增策略主要通过 policy/template 完成

### 5.5 Execution Layer

必须交付：

1. `position_lot ledger`
2. `order binding book`
3. `fill attribution engine`
4. `protection chain`
5. `flatten / orphan cleanup`
6. `reconciler`
7. `startup preflight`
8. `symbol kill switch`

要求：

1. 所有新股票策略共用这一层
2. broker 净仓不能直接被某个策略当作自己的仓位
3. 所有成交最终归到唯一 `position_lot_id`

### 5.6 Recovery / Cleanup Layer

必须交付：

1. startup residual reconcile
2. intraday residual cleanup
3. overnight cleanup route
4. DB fake order cleanup
5. DB fake position cleanup
6. runtime stale state reset

要求：

1. 不再依赖人工 SQL + 临时脚本组合
2. preview / execute / verify 三段必须标准化

### 5.7 Observability Layer

必须交付：

1. strategy domain audit
2. market domain audit
3. exec domain audit
4. recon domain audit
5. cleanup domain audit
6. startup / preflight metrics

要求：

1. 能解释为什么开仓
2. 能解释为什么没开仓
3. 能解释哪个 gate / feature / data 挡住了
4. 能解释 broker/DB/runtime 哪层有问题

## 6. 当前差距结论

当前 `stocks_kernel` 方向正确，但还不满足“任意股票策略直接生产可用”的要求。

### 6.1 当前已落地进度

已完成的基础平台件：

1. `position_lot ledger / fill attribution / protection chain`
2. startup preflight / symbol kill switch
3. residual cleanup tool + overnight cleanup route
4. strategy / market / exec / recon audit 主链
5. gate registry 已接入主流程
6. feature registry 已升级为 plugin + dependency + per-build cache
7. data provider / evaluator / submit bridge 已接入主流程
8. policy contract 已扩到 stop/tp/retreat/fill feedback 骨架
9. live submit 已接到 `open_entry_lot -> order_ref/position_lot_id -> broker ack`
10. strategy template registry 已落第一版
11. runtime 已支持 order update / position update 回报入口

仍未完成的核心平台件：

1. policy contract 仍缺 add plan 的真实行为和完整 live exit 执行
2. live execution 仍未完整回写到统一 lot execution kernel（fill/db persist -> lot）
3. Builder 模板层还只是第一版 registry，尚未形成交易员直接实例化产品
4. strategy shell 还可以继续瘦身

主要差距：

1. policy contract 不完整，exit/stop/flatten 仍未真正平台化
2. live execution 仍未完全由 stocks kernel execution kernel 接管
3. gate 和 feature 仍未完全注册化 / 声明式
4. data layer 仍偏薄，更多是 wrapper，不是完整 provider 平台
5. strategy shell 仍然过厚，编排逻辑集中
6. Builder 还没有成为交易员可直接使用的产品层

## 7. 阶段性决策

### 7.1 现在允许做的事情

1. `stocks_kernel` 在新 symbol 上做独立 paper
2. `stocks_kernel` 做 shadow/replay/parity
3. 持续做平台层升级

### 7.2 现在禁止做的事情

1. 用 `stocks_kernel` 再碰 `QQQ/TQQQ/SQQQ` live
2. 迁移 `qqq_intraday_001` 到新平台
3. 旧 `qqq_intraday_001` 和新平台同票 live 并行

## 8. TODO List

### P0: 平台化硬边界

1. gate registry 重构
   - 目标：去掉最终版 `if/elif` gate factory
   - 输出：注册式 gate loader + gate config schema

2. feature plugin 化
   - 目标：把 feature 从单文件内置函数提升为独立插件
   - 输出：feature plugin registry + dependency graph + cache

3. data provider 抽象
   - 目标：把 realtime/history/backfill/session 变成正式 provider
   - 输出：`RealtimeFeatureProvider/HistoryProvider/BackfillProvider/SessionResolver`

4. strategy shell 瘦身
   - 目标：让 runtime 只做 orchestration，不再堆策略实现细节
   - 输出：拆分 snapshot builder / evaluation runner / submit bridge

### P1: 完整 policy 平台化

5. policy contract 扩展
   - 输出：entry/add/stop/take_profit/exit/retreat/fill_feedback

6. `qqq` 完整策略逻辑抽象为 policy template
   - 注意：只抽到新平台，不替换旧 `qqq`

7. strategy template 系统
   - 输出：
     - trend_follow
     - breakout
     - mean_revert
     - session_relay

### P1: 执行层统一

8. live path 接入 position lot bridge
   - 目标：让 broker ack / fill / db persist 真正回写 lot ownership

9. reconcile 正式化
   - 输出：startup / intraday / overnight residual reconcile 标准流程

10. cleanup validation bundle
   - 输出：cleanup 前后 broker / DB / runtime 一致性 bundle

### P2: 生产接线

11. startup failure metrics 告警接线
12. Builder 文档和实例模板
13. preflight runbook 自动化

## 9. 测试方案

### 9.1 单测

1. gate registry 能按配置装配不同 gate 组合
2. feature dependency graph 正确解析与缓存
3. policy contract 能输出完整 entry/exit/stop/tp/retreat
4. fill attribution 唯一归属到 `position_lot_id`
5. startup preflight 在 broker/DB/runtime 脏状态下 100% 拒绝
6. cleanup tool preview / execute / verify 正确

### 9.2 集成测试

1. 新 symbol 独立 paper 运行
2. startup blocked -> no submit
3. broker ack -> fill -> db persist -> lot update 全链路存在
4. residual cleanup 一次恢复 broker/DB/runtime 一致

### 9.3 回放测试

1. 用历史 `qqq` 数据 replay 新 policy
2. 对比：
   - gate
   - entry
   - exit
   - stop
   - flatten

### 9.4 稳定性测试

1. 连续多交易日 paper 跑新 symbol
2. 无 residual escalation
3. 无 broker/DB/runtime 持续不一致
4. cleanup tool 在异常场景下可收敛

## 10. 验收标准

### 10.1 平台核心验收

1. 新策略实例新增时，不需要改 execution/reconcile/cleanup 主链
2. 新策略实例新增时，只允许改：
   - config
   - policy/template
   - 可选 feature plugin
   - 可选 gate plugin
3. startup preflight 脏状态下必须拒绝 live
4. symbol kill switch 生效后必须阻止重复 submit
5. cleanup tool 必须一次恢复 broker/DB/runtime 一致

### 10.2 Builder 验收

1. 交易员新增一个股票策略时，不需要写 runtime 主流程代码
2. 至少支持通过配置装配：
   - data profile
   - feature set
   - gate pipeline
   - policy template
   - universe
3. 至少有 3 个模板可用于不同风格股票策略

### 10.3 生产验收

1. 新 symbol 独立 paper 连续多个交易日稳定
2. 无异常 residual 累积
3. 无反复开仓 / 反向开仓 / 跨策略认仓
4. 任一故障都能从 audit 中完整还原

## 11. 最终判断标准

只有当以下两条同时成立，才能说目标达成：

1. Platform Core 已经稳定并统一接管股票策略执行基础设施
2. Strategy Builder 已经让交易员主要通过配置和模板创建策略实例

否则，都不能宣称“任意股票策略直接生产可用的通用框架”已经完成。
