# IBKR 重连治理最终方案

> Status: superseded as the primary execution document by `docs/implementation/IBKR_ORDER_TRUTH_AND_RECONNECT_FINAL_PLAN_2026-04-17.md`.
>
> Reason: this document correctly narrows reconnect ownership, but it does not fully cover:
> 1. report-only reconciler sequencing before clientId hardening
> 2. master truth session and startup adoption as first-class architecture
> 3. order identity extension with `ib_client_id` / `ib_session_role` / `ib_connection_epoch`
> 4. quarantine-based handling of unknown broker orders

## 背景

当前问题不是单一的 timeout、clientId 或 backoff 参数问题，而是 **连接所有权分散**、**恢复状态机分散**、**启动成功定义错误** 三个根因叠加造成的。

系统现在已经做过多轮局部修复，但仍反复出现以下现象：

- 非交易时段或异常路径出现多路 reconnect / probe / auxiliary session。
- IB 重启或网络中断后，部分组件认为自己应该发起恢复，形成重复连接或错误退避。
- `run_system.py` 在 broker 未真正 ready 时仍可能把系统标记为 running。
- IB 恢复后，系统不能稳定收敛到唯一健康状态。

这份文档的目标不是继续调参数，而是定义 **从第一性原理出发的最终收口方案**。

## 第一性原理

IB 集成要稳定，必须同时满足以下 4 条系统不变量：

1. **唯一连接所有者**
   - 整个代码库只能有一个模块有权创建、销毁、恢复 IB session。
   - 其他模块只能读状态、拿引用、注册回调，不能自行 `connect/start/reconnect`。

2. **唯一恢复状态机**
   - 断连后，只有一个恢复状态机负责：
     - 等待端口恢复
     - 串行发起握手
     - 失败退避
     - 恢复后广播 ready
   - 业务模块不得并行做自己的恢复决策。

3. **未 ready 时系统必须静默**
   - broker 未 ready 时，execution / reconciler / strategy 不得进入 operational state。
   - 允许启动进程，但不允许出现“系统已运行、broker 未连接”的假健康状态。

4. **恢复必须收敛**
   - IB 恢复后，系统必须收敛到固定且可解释的健康态：
     - `market ready`
     - `exec ready`
     - `trading enabled`
   - 不能出现长期 degraded、循环 denial、多个 caller 轮流争抢恢复权。

只要这 4 条里有一条不成立，就不能宣称“连接风暴已解决”。

## 对现有计划的 critical review

之前的计划方向是对的，但有 5 个不足：

1. **没有把“系统不变量”放在最前面**
   - 容易让后续修复重新退化为 timeout / retry 参数调优。

2. **没有区分“连接限流”和“连接所有权”**
   - gatekeeper 只能限流，不能替代状态机。
   - 只做 gatekeeper，不做 ownership 收口，问题还会复发。

3. **没有把启动路径和恢复路径分开**
   - 冷启动失败和运行中断连恢复的约束不同。
   - 二者共用一套“谁先启动谁算成功”的逻辑会继续产生假运行态。

4. **没有给出迁移顺序**
   - 如果先调 probe/backoff，再收口 legacy adapter，代码会继续有旁路入口。

5. **没有显式定义回滚点**
   - 这类改动跨度大，没有回滚点很容易在上线中间态卡住。

## 当前仍然成立的根因

### 根因 A：连接所有权没有收口

当前仍然存在以下可发起 IB 生命周期的入口：

- `IBConnectionManager`
- `IBInsyncAdapter.start()`
- legacy `get_ib_adapter()` / `init_global_ib_adapter()`
- `options_data` 的按需 `adapter.start()`
- `order_monitor` 的 legacy adapter 获取
- `vix_ingestor` 的 raw `IB()` + `connect()`

这意味着系统中不止一个“连接工厂”。

### 根因 B：恢复状态机没有收口

当前不是单一恢复流，而是：

- `market` 在尝试恢复
- `exec` 在后台 `_reconnector()` 尝试恢复
- `MarketDataService` 也在根据 adapter 状态决定重启流程
- 业务层还有部分 on-demand `start()`

这会导致：

- 恢复动作重复
- gatekeeper denial 被误当成连接失败
- 状态机长期抖动

### 根因 C：启动成功定义仍然偏向“任务已启动”

即便已经加了 startup gate，broker readiness 检查仍发生在 execution / strategy / reconciler 启动之后。  
这违反了“未 ready 时系统静默”的要求。

### 根因 D：诊断探针仍可能放大请求

辅助 probe / owner-cancel / fresh-probe 这类诊断路径不是主交易路径。  
它们必须满足：

- 低上限
- 有节流
- 有 in-flight dedupe
- 不得主导连接生命周期

否则 RTH 下仍可能产生 per-second auxiliary session。

## 目标架构

### 1. 单一 broker supervisor

将 `IBConnectionManager` 升级为唯一 broker supervisor：

- 唯一允许创建 market session
- 唯一允许创建 exec session
- 唯一允许触发 reconnect
- 唯一维护 broker state machine

### 2. 显式状态机

建议状态机：

- `DISCONNECTED`
- `WAIT_PORT`
- `CONNECTING_MARKET`
- `MARKET_READY`
- `CONNECTING_EXEC`
- `READY`
- `DEGRADED`

状态转换规则：

- 冷启动必须从 `DISCONNECTED -> WAIT_PORT` 开始。
- 先 market，后 exec。
- 任何真实断连事件都回到 `DISCONNECTED` 或 `WAIT_PORT`。
- 只有 `READY` 才允许 trading enabled。

### 3. 业务层全部改为消费者

以下模块只能消费状态，不能发起连接：

- `run_system`
- `MarketDataService`
- `ExecutionService`
- `RuntimeReconciler`
- `OrderMonitor`
- `options_data`
- `AccountService`
- `vix_ingestor`

## 分阶段落地顺序

### Phase 0：先收口入口，不调参数

目标：先消灭旁路入口。

要做的事：

- 禁止 legacy `get_ib_adapter()` 自建 adapter。
- `init_global_ib_adapter()` 改成 manager 的兼容壳。
- 替换所有直接 `get_ib_adapter()` 的业务调用点。
- `vix_ingestor` 不再直接 `IB()` + `connect()`。

完成标准：

- 代码库中不再有业务路径直接新建 `IB()` 并发起 `connect()`。
- 只有 `IBConnectionManager` 能创建活跃会话。

### Phase 1：前移启动门槛

目标：broker 未 ready 前，execution / reconciler / strategy 不上线。

要做的事：

- `run_system.py` 先等待 broker supervisor ready。
- 如果未 ready：
  - 默认直接失败退出
  - 或显式 degraded bootstrap，但必须 `trading_enabled=False`
- summary 日志必须反映真实 broker state。

完成标准：

- 不再出现“ALL SERVICES STARTED”但 broker 未连上的情况。
- 不再出现 execution service active 但 exec IB disconnected 的情况。

### Phase 2：统一恢复状态机

目标：market / exec 不再各自恢复。

要做的事：

- 将 reconnect owner 固定为 broker supervisor。
- `IBInsyncAdapter._reconnector()` 改成观察状态或被动等待，不再是主恢复者。
- `MarketDataService` 仅响应 state change，不自己做 reconnect orchestration。

完成标准：

- 断连后日志里只有一条主恢复流。
- 不再出现多个 caller 轮流拿 gatekeeper 的情况。

### Phase 3：收敛诊断探针

目标：诊断路径不再放大 broker 压力。

要做的事：

- fresh probe / owner cancel probe 增加 RTH floor（建议 1-2 秒）。
- 增加 per-order / per-client in-flight dedupe。
- 所有辅助 probe 都标记为 diagnostic，不得改变主状态机。

完成标准：

- RTH persistent anomaly 下，不会每秒创建新辅助 session。

### Phase 4：统一健康广播

目标：业务层不再靠猜测判断 broker 是否可用。

要做的事：

- broker supervisor 提供统一健康快照：
  - `state`
  - `market_connected`
  - `exec_connected`
  - `trading_enabled`
  - `last_failure_reason`
  - `next_retry_sec`
- 业务模块只订阅该健康态。

完成标准：

- 所有业务层 readiness 判断都来自同一个 health source。

## 测试计划

### 单元测试

1. gatekeeper
- 只允许一个 connect owner
- backoff 不持有 slot
- stale slot 可回收
- denial 不会伪装成 success

2. connection manager
- market 未 ready 时 exec 不启动
- exec 未 ready 时 manager 不返回伪 healthy adapter
- recovery path 串行

3. adapter
- timeout 后重建 `IB()` client
- RTH probe 有节流和 dedupe
- denial 不递增错误的 failure counters

4. startup
- broker 未 ready 时 summary 不打印 ready
- `RUN_SYSTEM_REQUIRE_IB_READY=1` 时进程失败退出

### 集成测试

1. 冷启动，IB 正常
- market 先 ready
- exec 后 ready
- execution / strategies 最后启动

2. 冷启动，IB 未启动
- 不进入 trading enabled
- 不出现多 clientId churn
- 恢复后自动进入 `READY`

3. 运行中 IB 重启
- 系统只保留单一恢复 owner
- 恢复后 market / exec 都回到 ready

4. 运行中断网再恢复
- 不产生连接风暴
- 自动回到 ready

5. RTH 异常订单
- diagnostic probe 不会每秒开新 session

### 实机验收

必须做 3 次真实场景：

1. `run_system` 冷启动 + IB 正常
2. `run_system` 运行中重启 IB
3. `run_system` 运行中断网后恢复

每次都记录：

- IBKR 客户端连接面板
- trader.log 关键时间线
- clientId 数量和变化
- 最终 broker state

## 验收标准

- [ ] 代码库中只有 `IBConnectionManager` 能创建活跃 IB session
- [ ] 不再有业务模块直接 `connect/start/reconnect`
- [ ] broker 未 ready 时，execution / strategy / reconciler 不进入 operational state
- [ ] 不再打印假 ready / 假 running 日志
- [ ] RTH 下 diagnostic probe 不会按 1 秒节奏创建辅助 session
- [ ] IB 重启后能够自动恢复，无需人工重启系统
- [ ] 断网恢复后能够自动恢复，无需人工重启系统
- [ ] 恢复过程中不出现 clientId 高频 churn
- [ ] 不出现持续性 `gatekeeper_denied` 抖动导致的假恢复
- [ ] 最终系统能稳定收敛到 `READY`

## 回滚策略

每个 phase 都必须可单独回滚：

- Phase 0 回滚：恢复 legacy adapter 兼容，但保留 gatekeeper 日志
- Phase 1 回滚：恢复旧启动顺序，但保留真实状态 summary
- Phase 2 回滚：恢复局部 reconnect，但保留 ownership 封锁
- Phase 3 回滚：仅回退 probe 限流参数，不回退统一状态机

原则：

- 不允许一次性混入“入口收口 + 状态机重构 + probe 调优”三类改动。
- 每个 phase 单独提交、单独测试、单独验收。

## 结论

后续不应再把重点放在：

- 调大/调小 timeout
- 修改单个 caller 的 sleep
- 继续增加新的 gatekeeper 特判

这些只能缓解，不能从根上证明系统稳定。

真正的完成标准只有一个：

**系统中只能有一个地方决定何时允许存在 IB 会话，以及何时允许交易恢复。**
