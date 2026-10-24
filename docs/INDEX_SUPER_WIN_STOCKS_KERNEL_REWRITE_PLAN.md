# INDEX_SUPER_WIN Stocks Kernel Rewrite Plan

## 1. 目标

本方案不再继续修补现有 `index_super_win_replica_*`。

新的目标是：

1. 重写一个独立的股票通用策略内核
2. 先用它 100% 复刻 `qqq_intraday_001` 的策略行为
3. 再让未来任意股票策略只通过“配置 + policy + gate + feature”搭建
4. 持仓、订单、保护单、IBKR 同步、对账、审计全部由内核统一处理
5. 不能影响现有 `qqq_intraday_001`、期权策略和其他现网策略


## 2. 核心判断

当前 `index_super_win_replica_strategy` 不适合作为通用策略底座，原因如下：

1. 它仍然深度依赖 `qqq_intraday_*` 专用逻辑
2. 它不是标准插件式内核，而是“qqq 逻辑搬运 + 多 symbol 容器”
3. 数据、特征、gate、执行、持仓同步的边界没有彻底拆开
4. 继续在此基础上演进，会把 `qqq` 的历史包袱固化成“通用框架”

结论：

1. 旧 `index_super_win` 冻结
2. 新建 `stocks_kernel`
3. `index_super_win` 以后只是新内核上的一个实例名，不再代表旧实现


## 3. 最高约束

### 3.1 零影响现网

1. 不修改现有 `qqq_intraday_001` 的交易行为
2. 不修改现有 `qqq_intraday_001` 的订单命名空间
3. 不修改现有 `qqq_intraday_001` 的 runtime state 文件格式
4. 不改变现有执行链对 `qqq_intraday_001` 的认单逻辑
5. 新内核默认不自动接入 `run_system`

### 3.2 安全隔离

1. 不复用 `qqqv1:*` 命名空间
2. 不共用旧 `index_super_win` 的 runtime 文件
3. 不允许新内核策略直接认领未知 broker 仓位
4. 不允许一个策略 flatten 另一个策略的仓位
5. 不允许一个策略撤掉另一个策略的保护单
6. 第一版必须支持“同 symbol 多策略同时真实持仓”
7. 同 symbol 多策略并行时，不能退化为账户净仓单一视角

### 3.3 架构边界

1. 策略 policy 只定义交易逻辑，不定义账户真相
2. 执行内核统一负责订单、持仓、保护单、同步
3. 数据层统一负责实时数据、历史数据、回补、freshness
4. 特征层统一负责指标依赖和 warmup
5. gate 层统一负责“为什么开”或“为什么不开”

### 3.4 真相源优先级

新内核必须把真相源优先级固定为：

1. `broker execution / broker position / broker open order`
2. `DB ledger / order_event / fill_event`
3. `position_lot ledger`
4. `runtime snapshot`
5. `strategy local memory`

任何层级冲突时，必须按上面顺序收敛，禁止自由选择真相源。

### 3.5 第一版并行持仓硬约束

第一版必须从设计上直接支持：

1. 同 symbol 不同策略同时真实持仓
2. 同 symbol 同方向双策略同时持仓
3. 同 symbol 反方向双策略同时持仓
4. 同 symbol 一个策略部分止盈，另一个策略补仓
5. 同 symbol 多策略各自拥有独立保护单
6. 账户净仓与单策略毛仓不相等时，系统仍能正确归因


## 4. 新架构

建议新增目录：

```text
strategies/stocks_kernel/
  base.py
  types.py
  config_loader.py
  runtime/
  data/
  features/
  gates/
  execution/
  policies/
```

### 4.1 Base Layer

`EquityKernelStrategy(StrategyAlgorithm)`

职责：

1. 符合现有 `StrategyEngine` 加载方式
2. 管理生命周期
3. 装配 data/features/gates/policy/execution
4. 分发 `on_features` / `on_order_update` / `on_position_update`
5. 只使用现有 `StrategyContext` 契约，不绕开 `run_system`

现有引擎接入契约必须固定：

1. 通过 `StrategyEngine.load_strategy()` 正常加载
2. 通过 `ctx.subscribe_features()` 订阅特征
3. 通过 `ctx.submit_intent()` 提交意图
4. 通过 `on_order_update()` / `on_position_update()` 接收 broker 回报
5. 不引入第二套并行 strategy engine

### 4.2 Data Layer

职责：

1. 统一接入实时数据
2. 统一接入 ClickHouse 历史数据
3. 统一处理开盘前 warmup
4. 统一处理 IBKR 历史回补
5. 统一处理 freshness/readiness

输出：

1. `MarketSnapshot`
2. `HistoryBundle`
3. `DataReadinessReport`

### 4.3 Feature Layer

职责：

1. 特征插件注册
2. 特征依赖声明
3. warmup 需求声明
4. 基于 `MarketSnapshot` 和历史数据计算 `FeatureContext`

首批通用特征：

1. `vwap`
2. `atr_eff`
3. `v3m`
4. `pos_day`
5. `vol_surge`
6. `opening_range`
7. `distance_from_vwap_atr`

### 4.4 Gate Layer

职责：

1. 标准 gate 输入输出
2. 统一 gate reason code
3. 统一 gate 审计
4. 支持按配置组合 gate pipeline

首批 gate：

1. `session_window`
2. `freshness`
3. `seed_ready`
4. `direction_match`
5. `risk_budget`
6. `open_orders_isolation`
7. `foreign_position_isolation`
8. `data_integrity`

### 4.5 Policy Layer

职责：

1. 定义策略 alpha 逻辑
2. 决定方向
3. 决定目标仓位
4. 决定加仓
5. 决定止盈止损和 retreat

不允许：

1. 自己提交订单
2. 自己管理 broker 仓位
3. 自己认定成交完成

首个 policy：

1. `qqq_intraday_policy_v1`

### 4.6 Execution Layer

职责：

1. 订单 identity
2. intent -> submit -> ack -> fill 状态机
3. `position_lot ledger`
4. protective stop
5. flatten
6. orphan order cleanup
7. broker snapshot sync
8. quarantine

这一层的唯一真相对象不是 `strategy_id + symbol`，而是：

`strategy_id + symbol + position_lot_id + decision_id`

### 4.7 Runtime / Audit Layer

职责：

1. runtime snapshot
2. restart recovery
3. audit trace
4. preview / decision / submit / fill / flatten 证据链
5. replay 输入输出标准化


## 5. 模块接口设计

### 5.1 配置输入

目标配置形式：

```yaml
strategy_id: qqq_intraday_kernel_001
engine: stocks_kernel
data_profile: qqq_rth_standard
universe:
  - signal_symbol: QQQ
    long_exec_symbol: TQQQ
    short_exec_symbol: SQQQ
feature_set:
  - vwap
  - atr_eff
  - v3m
  - pos_day
  - vol_surge
gate_pipeline:
  - session_window
  - freshness
  - seed_ready
  - direction_match
  - risk_budget
  - foreign_position_isolation
policy:
  id: qqq_intraday_policy_v1
  params:
    max_trades_per_day: 4
    hard_stop_atr_mult: 0.75
    logic_retreat_min_hold_sec: 360
execution_profile:
  namespace: stkv1
  protection_enabled: true
  quarantine_on_unknown_position: true
```

### 5.2 Policy 标准接口

`Policy` 只关心：

1. `build_signal()`
2. `build_entry_plan()`
3. `build_add_plan()`
4. `build_exit_plans()`

### 5.3 Gate 标准接口

每个 gate 输出：

1. `allowed`
2. `reason`
3. `trader_reason_code`
4. `payload`

### 5.4 Execution 标准接口

执行层对外暴露：

1. `preview_entry`
2. `submit_entry`
3. `preview_exit`
4. `submit_exit`
5. `ensure_protection`
6. `sync_from_broker`
7. `reconcile_open_orders`
8. `quarantine_position`

### 5.5 切流身份策略

重写期间禁止直接复用旧 `index_super_win_001` 身份。

执行规则：

1. 新内核 shadow / replay / paper 阶段使用独立 `strategy_id`
2. 新内核 shadow / replay / paper 阶段使用独立 namespace
3. 新内核 shadow / replay / paper 阶段使用独立 runtime snapshot 路径
4. 只有在完成最终验收后，才允许切回 `index_super_win_001`

建议：

1. shadow/paper 阶段使用 `qqq_intraday_kernel_001`
2. namespace 使用 `stkv1`
3. 最终替换时再决定是否回收 `index_super_win_001`

### 5.6 Position Lot Ledger 设计

第一版唯一真相源必须是 `position_lot ledger`。

每个 `position_lot` 必须至少包含：

1. `position_lot_id`
2. `strategy_id`
3. `symbol`
4. `side`
5. `opened_by_decision_id`
6. `opened_by_trigger_id`
7. `entry_order_ref`
8. `entry_order_id`
9. `entry_perm_id`
10. `live_qty`
11. `filled_qty`
12. `avg_entry_price`
13. `state`
14. `protection_order_refs`
15. `flatten_order_refs`

设计原则：

1. `symbol` 级视图只是 lot ledger 的索引视图，不是独立真相源
2. 保护单必须挂在 lot 上，不挂在 symbol 净仓上
3. flatten 只能针对具体 lot 或具体 lot 集合，不能默认针对账户净仓

### 5.7 Fill Attribution 设计

第一版必须单独实现 `fill attribution`，不能把它埋在 execution handler 里隐式处理。

归属优先级：

1. `perm_id`
2. `broker_order_id`
3. `order_ref`
4. `decision_id`
5. `parent_order_ref`
6. `fallback broker execution linkage`

归属规则：

1. 能唯一归属才允许更新 lot 状态
2. 不能唯一归属时进入 `quarantine`
3. 不能唯一归属时禁止修改任何策略 live_qty
4. 不能唯一归属时必须落 audit 证据

### 5.8 Protection Model 设计

第一版保护单模型采用：

1. `one lot -> one protection chain`

不采用：

1. 同策略同 symbol 聚合保护单
2. 账户净仓级保护单

原因：

1. 对同 symbol 多策略最安全
2. 归属最清晰
3. replay / audit 最容易还原


## 6. 目录与文件落地清单

### Phase 0: 文档和脚手架

1. 新增 `docs/INDEX_SUPER_WIN_STOCKS_KERNEL_REWRITE_PLAN.md`
2. 新增 `strategies/stocks_kernel/__init__.py`
3. 新增 `strategies/stocks_kernel/base.py`
4. 新增 `strategies/stocks_kernel/types.py`
5. 新增 `strategies/stocks_kernel/config_loader.py`

### Phase 1: Execution Kernel

1. `strategies/stocks_kernel/execution/order_identity.py`
2. `strategies/stocks_kernel/execution/ownership.py`
3. `strategies/stocks_kernel/execution/runtime_store.py`
4. `strategies/stocks_kernel/execution/coordinator.py`
5. `strategies/stocks_kernel/execution/protection.py`
6. `strategies/stocks_kernel/execution/broker_sync.py`
7. `strategies/stocks_kernel/execution/reconciler.py`

### Phase 2: Data / Feature Kernel

1. `strategies/stocks_kernel/data/market_snapshot.py`
2. `strategies/stocks_kernel/data/history_loader.py`
3. `strategies/stocks_kernel/data/backfill_coordinator.py`
4. `strategies/stocks_kernel/data/readiness.py`
5. `strategies/stocks_kernel/features/registry.py`
6. `strategies/stocks_kernel/features/vwap.py`
7. `strategies/stocks_kernel/features/atr_eff.py`
8. `strategies/stocks_kernel/features/v3m.py`
9. `strategies/stocks_kernel/features/pos_day.py`
10. `strategies/stocks_kernel/features/vol_surge.py`

### Phase 3: Gate Pipeline

1. `strategies/stocks_kernel/gates/base.py`
2. `strategies/stocks_kernel/gates/pipeline.py`
3. `strategies/stocks_kernel/gates/session_window.py`
4. `strategies/stocks_kernel/gates/freshness.py`
5. `strategies/stocks_kernel/gates/seed_ready.py`
6. `strategies/stocks_kernel/gates/direction.py`
7. `strategies/stocks_kernel/gates/risk_budget.py`
8. `strategies/stocks_kernel/gates/open_orders_isolation.py`
9. `strategies/stocks_kernel/gates/foreign_position_isolation.py`

### Phase 4: Policy 层

1. `strategies/stocks_kernel/policies/base.py`
2. `strategies/stocks_kernel/policies/qqq_intraday_policy_v1.py`
3. `strategies/stocks_kernel/policies/templates/`
4. `strategies/stocks_kernel/policies/registry.py`

### Phase 5: Audit / Replay / Tooling

1. `strategies/stocks_kernel/runtime/audit.py`
2. `strategies/stocks_kernel/runtime/snapshot.py`
3. `strategies/stocks_kernel/runtime/recovery.py`
4. `tool/replay/run_stocks_kernel_replay.py`
5. `tool/report/build_stocks_kernel_parity_report.py`


## 7. 详细 TODO

## 7.1 Phase 0: 冻结旧实现

- [ ] 确认旧 `index_super_win_replica_*` 不再作为新增功能入口
- [ ] 确认 `config.yml` 中没有新的自动切流动作
- [ ] 固化当前 `qqq_intraday_001` 真实交易链路样本
- [ ] 固化当前 `qqq_intraday_001` 开仓、加仓、止损、flatten 样本
- [ ] 固化当前同 symbol 多策略隔离基线

## 7.2 Phase 1: 新内核脚手架

- [x] 新建 `stocks_kernel` 目录
- [x] 新增统一类型定义
- [x] 新增统一 strategy base
- [x] 新增配置装配器
- [x] 新增内核最小初始化测试

## 7.3 Phase 2: 执行与持仓基础设施

- [x] 新增独立 namespace 规范，禁止复用 `qqqv1`
- [x] 新增统一 `order_ref` parser/builder
- [ ] 定义切流身份策略：shadow id / live id / 回收旧 id 规则
- [x] 新增 `position_lot ledger`
- [x] 新增 `symbol` 级索引视图，明确其来源于 lot ledger
- [x] 新增 broker snapshot reader
- [x] 新增 quarantine 规则
- [x] 新增 unknown broker position 拦截
- [x] 新增 foreign open order 拦截
- [x] 新增 fill attribution service
- [x] 定义 attribution 唯一归属规则
- [x] 定义 attribution 失败时的 quarantine 路径
- [x] 新增 protective stop manager
- [x] 明确第一版采用 `one lot -> one protection chain`
- [x] 新增 flatten coordinator
- [x] 新增 orphan cleanup coordinator
- [x] 新增 restart recovery coordinator
- [x] 定义同 symbol 同方向双策略并行 lot 模型
- [x] 定义同 symbol 反方向双策略并行 lot 模型

## 7.4 Phase 3: 数据底座

- [x] 定义 `MarketSnapshot`
- [x] 定义 `HistoryBundle`
- [ ] 接入实时行情 facade
- [x] 接入 ClickHouse 历史读取
- [ ] 接入开盘前 warmup 调度
- [ ] 接入 off-hours backfill 调度
- [x] 定义 `data_profile`
- [x] 定义 freshness/readiness 统一规则
- [x] 定义 fail-closed / fail-open 可配置策略

## 7.5 Phase 4: 特征层

- [x] 实现特征注册中心
- [ ] 支持按依赖拓扑执行
- [ ] 支持 warmup 需求声明
- [x] 实现 `vwap`
- [x] 实现 `atr_eff`
- [x] 实现 `v3m`
- [x] 实现 `pos_day`
- [x] 实现 `vol_surge`
- [x] 实现 `distance_from_vwap_atr`
- [ ] 实现特征 ready/not-ready 统一原因码

## 7.6 Phase 5: Gate Pipeline

- [x] 定义 gate 协议
- [x] 定义 gate runner
- [ ] 定义 gate 审计格式
- [x] 实现 `session_window`
- [x] 实现 `freshness`
- [x] 实现 `seed_ready`
- [x] 实现 `direction_match`
- [x] 实现 `risk_budget`
- [x] 实现 `open_orders_isolation`
- [x] 实现 `foreign_position_isolation`
- [x] 支持 gate 顺序可配置
- [x] 支持 gate 粒度启停

## 7.7 Phase 6: QQQ Policy 复刻

- [x] 建 `qqq_intraday_policy_v1`
- [ ] 把 entry 逻辑收敛到 policy
- [ ] 把 add 逻辑收敛到 policy
- [ ] 把 hard stop 逻辑收敛到 policy
- [ ] 把 tp1 / trail / retreat 逻辑收敛到 policy
- [ ] 把 session-specific 行为收敛到配置
- [ ] 统一 policy 输入为 `FeatureContext`
- [ ] 统一 policy 输出为 `SignalIntent / EntryPlan / ExitPlan`
- [ ] 明确 `policy parity` 与 `concurrency safety` 分开验收

## 7.8 Phase 7: 可观测性

- [x] 每个关键节点落 audit 事件
- [ ] 每个 gate 都要有 allow/block 证据
- [ ] 每次 entry/exit 都要能还原因果链
- [ ] 每次 protection 补单都要留证据
- [x] 每次 quarantine 都要留证据
- [x] 支持按 `trace_id/order_ref/strategy_id/symbol` 查询

## 7.9 Phase 8: Shadow / Replay / Paper

- [ ] 基于历史样本 replay 新内核 qqq policy
- [ ] 输出 parity report
- [x] 构建 dual-live-lot simulation
- [x] 构建同 symbol 双策略 fill stream 仿真
- [ ] 与现网 `qqq_intraday_001` 做 shadow
- [ ] 与现网同 symbol 并行运行
- [ ] 与其他策略同时跑 paper
- [ ] 连续多日验证后再考虑切换


## 8. 测试方案

## 8.1 单元测试

### A. Execution Kernel

1. `order_ref` 构造和解析稳定
2. namespace 不会误认 `qqqv1`
3. `preview_entry` / `submit_entry` / `preview_exit` / `submit_exit` 状态迁移正确
4. `position_lot ledger` 状态迁移正确
5. foreign position 进入 quarantine
6. foreign open order 不被认领
7. fill attribution 只能在唯一归属时更新 lot
8. fill attribution 无法唯一归属时进入 quarantine 且不改 live_qty
9. stop manager 不会为 foreign lot 补 stop
10. flatten coordinator 只会 flatten 自己的 lot
11. orphan cleanup 只处理本策略订单

### B. Data Layer

1. freshness 计算正确
2. backfill 缺失时 ready=false
3. warmup 完成后特征可用
4. off-hours 与 RTH 的 data profile 路径正确
5. 历史数据和实时数据拼接无时间倒流

### C. Feature Layer

1. 特征注册和依赖解析正确
2. 特征缺依赖时返回标准 not-ready
3. `vwap`、`atr_eff`、`v3m`、`pos_day` 输出稳定
4. 相同输入重复计算结果一致

### D. Gate Layer

1. gate 顺序稳定
2. gate allow/block 结果稳定
3. gate payload 和 trader reason code 一致
4. fail-closed / fail-open 行为符合配置

### E. Policy Layer

1. `qqq_intraday_policy_v1` 在标准样本上给出预期方向
2. entry / add / exit / retreat 输出稳定
3. policy 不直接触发 submit
4. policy 不直接读 broker state


## 8.2 集成测试

### A. 单策略单 symbol

1. 初始化 -> warmup -> gate -> submit -> fill -> stop -> flatten 全链路正确
2. 重启后 runtime 可恢复
3. IBKR snapshot 与 runtime 不一致时进入 quarantine

### B. 单策略多 symbol

1. 多 symbol 并行订阅不串状态
2. 每个 symbol 独立 gate
3. 每个 symbol 独立持仓和 stop
4. 一个 symbol 异常不影响其他 symbol

### C. 多策略同 symbol

1. 两个策略同时跑同一个 exec symbol 不会互平
2. 两个策略保护单不互相认领
3. orphan cleanup 不跨策略误清
4. ownership 冲突时行为可解释、可审计
5. 同方向双策略同时 entry 后，两个 lot 独立存在
6. 反方向双策略同时 entry 后，两个 lot 独立存在
7. 一个策略部分止盈，另一个策略补仓，lot 状态不串
8. 两个策略各自 stop fill 后，只有对应 lot 关闭
9. 账户净仓与策略毛仓不一致时，归属结果仍可解释

### D. 与现网策略并行

1. `qqq_intraday_001` 正常开单时，新内核不会误认它的仓位
2. `qqq_intraday_001` 正常补 stop 时，新内核不会误认它的 stop
3. 新内核下单时，现网 `qqq` 不会误认
4. 期权策略运行期间不受影响


## 8.3 Replay / Parity 测试

1. 选取 3 个正常交易日和 2 个异常交易日
2. 回放 `qqq_intraday_001` 的实际行情与关键链路
3. 比较以下结果：
   - gate allow/block
   - entry minute
   - add minute
   - hard stop 触发点
   - tp1/retreat/flatten 触发点
4. 对每笔差异输出 reason diff
5. 差异不能只看“是否成交”，必须看到“为什么不同”


## 8.4 Dual-Live-Lot Simulation

这是第一版必须新增的验证层。

场景至少包括：

1. 同 symbol 同方向双策略同时 entry
2. 同 symbol 反方向双策略同时 entry
3. 一个 lot partial fill，另一个 lot full fill
4. 一个 lot add，另一个 lot flatten
5. 两个 lot 各自补保护单
6. 一个 lot stop fill，另一个 lot 继续持有
7. orphan stop 仅影响所属 lot

验收要求：

1. 每个 fill 都能唯一归属或被明确 quarantine
2. 任何仿真阶段都不能出现 cross-strategy live_qty 污染
3. 任何仿真阶段都不能出现 cross-strategy flatten
4. 任何仿真阶段都不能出现 cross-strategy stop rebuild


## 8.5 Paper 验证

### Paper Phase A

1. 新内核单策略单 symbol
2. 不与 `qqq_intraday_001` 并行
3. 连续多天观察 orphan/stop/sync

### Paper Phase B

1. 新内核 `qqq policy` 与 `qqq_intraday_001` 并行
2. 同 symbol 运行
3. 比较成交、保护单、仓位、平仓链路

### Paper Phase C

1. 新内核多 symbol 同时运行
2. 新内核与其他股票策略并行
3. 新内核与期权策略并行


## 9. 验收标准清单

## 9.1 工程验收

- [ ] 新实现全部位于 `strategies/stocks_kernel/`
- [ ] 旧 `qqq_intraday_001` 不引入新内核依赖
- [ ] 旧 `index_super_win_replica_*` 不再承接新增功能
- [ ] 配置驱动可以装配 `data_profile + feature_set + gate_pipeline + policy + execution_profile`
- [ ] 新增一个股票策略不需要复制 execution/ownership/reconcile 代码

## 9.2 安全验收

- [ ] 不会把别的策略仓位认成自己的
- [ ] 不会把别的策略保护单认成自己的
- [ ] 不会因为重启丢状态而误 flatten
- [ ] 不会因为 broker 有未知仓位而继续盲目开仓
- [ ] 不会出现同 symbol 多策略互相平仓
- [ ] 不会出现重复开仓、反向开仓、幽灵订单导致的错误恢复

## 9.3 行为验收

- [ ] `policy parity` 样本中 gate allow/block 一致率 `>= 99.5%`
- [ ] `policy parity` 样本中 entry/add/flatten 分钟偏差 `<= 1 bar`
- [ ] `policy parity` 样本中 hard stop / tp1 / retreat 触发一致率 `>= 99.0%`
- [ ] gate reason 差异可解释
- [ ] 可通过审计链路解释“为什么开”或“为什么不开”

## 9.4 并行验收

- [ ] 单策略多 symbol 正常运行
- [ ] 多策略同 symbol 正常运行
- [ ] 与 `qqq_intraday_001` 同时运行不冲突
- [ ] 与其他股票策略同时运行不冲突
- [ ] 与期权策略同时运行不冲突
- [ ] `concurrency safety` 样本中 cross-strategy fill mis-attribution = `0`
- [ ] `concurrency safety` 样本中 cross-strategy flatten = `0`
- [ ] `concurrency safety` 样本中 cross-strategy stop rebuild = `0`
- [ ] `concurrency safety` 样本中 foreign order cancellation = `0`

## 9.5 运行验收

- [ ] 支持 RTH
- [ ] 支持盘前/盘后/夜盘 profile
- [ ] 支持历史回补和 warmup
- [ ] 支持 restart recovery
- [ ] 支持 quarantine
- [ ] 支持 replay
- [ ] 支持 paper 验证


## 10. 上线原则

1. 先 replay
2. 再 shadow
3. 再单策略 paper
4. 再同 symbol 双策略 paper
5. 最后才考虑替换旧 `index_super_win`

任何阶段只要出现以下问题，立即回退到上一阶段：

1. foreign position 误认
2. foreign order 误认
3. stop ownership 错乱
4. runtime / broker / DB 三方状态解释不通
5. 与 `qqq_intraday_001` 并行时出现互相影响


## 11. 本轮落地结论

本轮不再以“修复旧 `index_super_win`”为方向。

本轮正式切换为：

1. 冻结旧 replica 路线
2. 新建 `stocks_kernel`
3. 先抽基础设施
4. 再复刻 `qqq policy`
5. 最后把 `index_super_win` 作为新内核上的通用股票策略入口
