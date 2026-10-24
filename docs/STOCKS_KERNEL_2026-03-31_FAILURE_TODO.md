# Stocks Kernel 2026-03-31 Failure TODO

## 问题清单

1. `stocks_kernel_shadow_qqq_001` 在未通过并行隔离验收前直接进入真实 paper 下单。
2. 新内核曾错误对 `QQQ` 本体发单，而不是只把 `QQQ` 当信号腿。
3. broker / DB / runtime ctx 没有统一残留恢复流程。
4. `qqq_intraday_001` 以前只有异常告警，没有连续异常后的自动收敛。
5. 夜盘残留仓位的正式平仓路径缺失，普通 `outsideRth` 不够。

## 已落地

1. `stocks_kernel` 启用前硬门槛
   - 文件：`./strategies/stocks_kernel/strategy.py`
   - 行为：
     - `dry_run=false` 且 `startup_preflight_enabled=true` 时，启动前检查 broker / DB / runtime
     - broker 执行腿只要有 live position 或 open orders，直接 `startup_blocked`
     - DB 只要有残留 position / active order，直接 `startup_blocked`
     - 启动被拒绝后，后续 `on_features` / `submit_intent` 都直接跳过

2. `stocks_kernel` 配置支持 startup preflight 开关
   - 文件：`./strategies/stocks_kernel/config_loader.py`
   - 新字段：`startup_preflight_enabled`

3. `qqq_intraday_001` 自愈补丁
   - 文件：`./strategies/qqq_intraday_strategy.py`
   - 行为：
     - `ctx_nonzero_broker_flat` 连续探针后重置 stale ctx
     - `ctx_flat_broker_nonzero` 连续探针后 quarantine foreign residual
     - 同类 warning 收敛，不再无限刷屏

4. 统一残留清理工具
   - 文件：`./tool/cleanup_strategy_residuals.py`
   - 行为：
     - 读取 broker / DB 残留
     - 生成 broker flatten 计划
     - terminalize DB 活动单
     - zero DB 脏持仓
     - 可选 reset qqq runtime state
     - 支持 `overnight` 路由：`exchange=OVERNIGHT` + `tif=OVERNIGHT`

5. cleanup 工具接本地 recon 审计
   - 文件：`./tool/cleanup_strategy_residuals.py`
   - 事件：
     - `residual_cleanup_started`
     - `residual_cleanup_preview`
     - `residual_cleanup_completed`

6. `stocks_kernel` symbol-level kill switch
   - 文件：`./strategies/stocks_kernel/strategy.py`
   - 行为：
     - 某执行腿被 broker isolation 挡住后，当天直接 block 该 symbol
     - 不再反复进入 submit path

7. live path broker/DB 审计桥接补齐
   - 文件：`./services/exec/execution_service.py`
   - 行为：
     - 成功落库统一审计为 `db_persisted`
     - 已存在的 `broker_submit_ack` / `fill_received` / `db_persist_failed` 形成统一链路

8. `stocks_kernel` startup failure metrics
   - 文件：`./strategies/stocks_kernel/strategy.py`
   - 指标：
     - `stocks_kernel_startup_preflight_total`
     - `stocks_kernel_startup_blocked`
     - `stocks_kernel_symbol_kill_switch_total`

## 未完成

1. 给 startup failure metrics 加 runtime dashboard / alert 接线
   - 代码已有指标，但尚未接告警与看板

2. cleanup tool 输出 validation bundle
   - 目前有审计和 JSON 输出，还没有固定日报/收盘验收 bundle

3. 新内核启用前 runbook 自动化
   - preflight 已经有代码，但还没有一键“启用前检查 + 拒绝原因汇总”入口

## 下一步顺序

1. startup failure metrics 告警接线
2. cleanup validation bundle
3. preflight runbook 自动化

## 测试方案

1. 单测
   - preflight 遇到 broker live position 时拒绝启动
   - preflight 遇到 broker open order 时拒绝启动
   - preflight 遇到 DB residual 时拒绝启动
   - `startup_preflight_enabled=false` 时允许初始化，但仍保留提交前 broker isolation
   - cleanup tool 对 `QQQ -1 / SQQQ +1` 生成正确 overnight flatten 计划
   - cleanup tool 只清目标策略和非 terminal 订单

2. 集成测试
   - 构造 broker/DB/runtime 混乱场景后，cleanup 工具一次收敛到全 flat
   - `qqq_intraday_001` 在 `ctx!=broker` 的连续探针场景下自动收敛并停止刷 warning

## 验收标准

1. `stocks_kernel` 未通过 preflight 时，不能真实下单。
2. broker / DB / runtime 任一不干净时，系统能明确给出拒绝原因。
3. cleanup 工具能在一次流程中完成：
   - broker 挂单清理
   - broker 残仓 flatten
   - DB 活动单 terminalize
   - DB 脏持仓归零
   - runtime state reset
4. `qqq_intraday_001` 在残留冲突场景下不再无限刷 warning。
