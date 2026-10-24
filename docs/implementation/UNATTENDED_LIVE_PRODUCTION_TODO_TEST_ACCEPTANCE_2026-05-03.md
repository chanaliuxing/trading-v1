# 无人值守真钱生产上线 TODO / 测试方案 / 验收标准

日期：2026-05-03

目标：把当前 paper/shadow 基本可运行状态推进到“真钱无人值守可上线”。这里的“可上线”不是只看 `run_system` 能启动，而是要求 IB 连接、订单真相、仓位真相、策略数据面、安全订单、EOD 平仓、监控告警和回滚机制全部有可验证闭环。

## 当前判断

当前系统不应直接进入真钱无人值守。

已具备的基础：

- `run_system` 单实例锁已改为持久锁文件，只释放 flock，不删除路径，避免 inode 竞态。
- ExecutionService / UnifiedOrderManager / RuntimeReconciler 可以启动。
- 当前 DB 检查显示 `core.core_position` 净仓为 0，working orders 为 0，ledger 按 `strategy_id/symbol` 汇总净仓为 0。
- IB market/exec probe 正常，open order snapshot 为 0。
- QQQ 主循环 heartbeat 正常，`startup_positions_ready=true`、`broker_positions_ready=true`。

仍阻止真钱无人值守的关键缺口：

- 启动 summary 仍可能只因 IB/execution ready 就打印 `ALL SERVICES STARTED - SYSTEM RUNNING`，没有纳入策略数据面、seed、runtime reconciler clean result、data-unavailable gate。
- startup smoke 仍未走真实 `StrategyEngine.load_strategy(...)` 路径；它只能证明入口 import、部分 class import 和 manager 构造没有崩，不能证明 enabled strategies 能按生产配置加载到运行中的 StrategyEngine。
- 生产启动当前仍可能吞掉单个 strategy load failure 并继续；如果 enabled live strategies 最终 loaded 数为 0，系统仍可能进入“进程健康但功能死亡”的状态。
- `bars_async_fallback_used rows=0` 高频 warning 仍然只是 warning；RTH 中应该变成明确 data-unavailable health gate，并阻止 entry。
- QQQ heartbeat 盘后显示 `stream_invalid=true`、`seed_ready=false`。盘后可接受，但不能作为 RTH 上线证据。
- 当前工作区仍有未提交或未归类文件，部署输入不干净。

## 上线原则

1. 任何 entry 可以被数据、health、admission、risk 阻止；任何 safety action 不应被这些普通 entry gate 阻止。
2. `system_running` 不能只代表进程活着，必须代表 broker/order/position/data/strategy 都满足生产门禁。
3. 数据不可用必须显式进入 health state 或 strategy entry gate，不能用空数组、默认值或 silent skip 继续。
4. IB 重启、网络断开、ClickHouse 短故障、策略重启后，最终状态必须收敛到 `running + broker/order/position truth clean` 或 `halted/quarantine/manual lock`，不能停在中间态。
5. 平仓、保护、撤单必须幂等，重复 reconcile 不得堆叠 safety orders。
6. 启动必须 fail closed：mandatory 阶段失败时只能 `HALTED` 或 `NON_TRADING_DEGRADED`，不能 log error 后继续假装可交易。
7. 生产 truth 顺序必须固定：broker truth / order truth / position ledger truth 优先，strategy memory 不能作为恢复真相来源。

## 生产启动状态机

生产启动必须显式记录状态转移：

```text
BOOTING
  -> INFRA_READY
  -> BROKER_READY
  -> EXEC_READY
  -> STRATEGY_DRY_LOADED
  -> STRATEGIES_LOADED
  -> RECONCILED
  -> RUNNING
```

失败状态：

```text
HALTED
NON_TRADING_DEGRADED
```

mandatory 阶段失败规则：

- `INFRA_READY` 失败：不能交易；进入 `HALTED` 或 `NON_TRADING_DEGRADED`。
- `BROKER_READY` 失败且 trading mode 是 paper/live：不能交易。
- `EXEC_READY` 必须先证明 ExecutionService / UOM / DecisionPipeline safety lane 可用。
- `STRATEGY_DRY_LOADED` 只能使用 no-op submit path；它不能单独贡献 `production_ready=true`。
- enabled live strategy 一个都没 load：不能交易。
- mandatory live strategy load 失败：不能交易，除非配置显式允许该策略 non-blocking。
- ExecutionService / UOM / RuntimeReconciler 初始化失败：不能交易。
- phase0/UOM gate 初始化失败：不能交易。
- IB exec/master truth 不可用且 trading mode 是 paper/live：不能交易。
- RuntimeReconciler dirty：不能 `RUNNING`。

## TODO

### P0 - 上线硬门禁

- [x] 固定测试入口到生产运行时：
  - `make test` 使用 `./venv_ml/bin/python -m pytest` 或等价固定解释器。
  - `make test` 必须包含 startup smoke。
  - 测试报告输出 Python path、Python version、venv path。
- [x] 建立第一类 strategy dry-load contract：
  - `DryRunStrategyContext`
  - `NoopMarketDataFacade`
  - `NoopDecisionPipeline`
  - `NoopEventBus`
  - `NoopHealthController`
  - `NoopSubmitIntent`
  - dry-load 不允许连接 IB/Kafka/ClickHouse。
  - dry-load 不允许真实下单、撤单、改单。
  - dry-load 必须走 `StrategyEngine.load_strategy(...)` 或 dedicated dry-load API。
- [x] 建立 `ProductionReadinessGate` 或等价 startup gate，`run_system` 最终 summary 必须区分：
  - `PROCESS_STARTED`
  - `BROKER_API_READY`
  - `EXECUTION_READY`
  - `STRATEGY_DRY_LOADED`
  - `RUNTIME_RECONCILED`
  - `STRATEGY_DATA_READY`
  - `PRODUCTION_READY`
- [x] `ALL SERVICES STARTED - SYSTEM RUNNING` 不能作为真钱上线信号；新增明确日志事件 `production_ready=true/false`。
- [x] `run_system` 启动必须 fail closed：
  - 不再允许 mandatory component `log error then continue`。
  - 任一 mandatory live strategy load 失败时 halt/degraded。
  - enabled live strategy loaded count = 0 时 halt/degraded。
  - ExecutionService / UOM / RuntimeReconciler 初始化失败时 halt/degraded。
  - phase0/UOM gate 初始化失败时 halt/degraded。
  - IB exec/master truth 不可用且 trading mode 是 paper/live 时 halt/degraded。
- [x] RuntimeReconciler clean result 必须进入生产门禁：
  - ghosts = 0
  - orphans = 0
  - order mismatches = 0
  - position mismatches = 0
  - active quarantine = 0
- [x] StartupReconciler 必须把 active quarantine 作为冷启动 evidence：
  - `active_quarantine_count` 必须来自 `core.core_order_quarantine`。
  - 缺失 quarantine evidence 不得默认 clean。
  - active quarantine > 0 时禁止 `production_ready=true`。
- [x] DecisionPipeline 必须进入生产门禁：
  - paper/live 下 `DecisionPipelineService` 初始化失败必须 fail closed。
  - `production_ready=true` 必须要求 decision pipeline 与 UOM execution bridge ready。
- [x] IB restart/resume 只能先进入 `broker_api_ready/recovering`，最终 `system_resumed` 必须由 RuntimeReconciler clean run 发布。
- [x] Runtime resume 必须纳入平台 recovery readiness：
  - broker/order/position/quarantine clean 只是必要条件。
  - market data service ready。
  - IB market data subscriptions ready，且必须覆盖 live strategy configured required symbol set，不能只要求任意一个 active subscription。
  - tick producer ready。
  - 5s bar pipeline ready。
  - RTH data availability 没有 entry-blocking gate。
  - 任一 required component 不 ready 时保持 recovering，不发布 `system_resumed`。
- [x] Recovery readiness lease 必须连续保护 running 状态：
  - market-data/bar/tick readiness 有 TTL/freshness，不是一次性状态位。
  - RuntimeReconciler 在 running 状态下发现 required component stale/not-ready 时发布 `system_degraded`，阻断 entry，但保留 safety lane。
  - required component not-ready 事件会触发 RuntimeReconciler 复核，不需要等下一次 broker event。
- [x] Recovery readiness 必须能反向触发 resume：
  - required component 从 not-ready 变 ready 时通知 RuntimeReconciler。
  - RuntimeReconciler 在 `platform_recovery_readiness_not_met` 后自动短延迟复核，防止恢复事件丢失后永久卡在 recovering。
  - market-data reconnect 完成后不需要新的 IB broker event 也能重新评估并发布最终 `system_resumed`。
- [x] Bar pipeline readiness 必须包含 Kafka producer 健康：
  - 5s aggregator producer start 成功才可标记 bar pipeline ready。
  - Kafka send / producer recovery 失败必须标记 bar pipeline not-ready。
  - producer recovery 成功后重新标记 ready 并触发 runtime resume 复核。
- [x] Cold-start `system_resumed` 不得绕过 platform recovery readiness：
  - startup `system_resumed` metadata 必须包含 `recovery_readiness` 与 `platform_recovery_ready`。
  - platform readiness 未满足时 startup 发布 `system_recovering`，不能直接 running。
  - SystemHealthController 对缺失或 false 的 `platform_recovery_ready` / `recovery_readiness.ready` resume 事件必须保持 recovering。
- [ ] 引入第一类 `RuntimeRecoveryCoordinator` artifact：
  - 单一 `recovery_id`。
  - broker events。
  - market-data lifecycle events。
  - reconciler reports。
  - health transitions。
  - submitted safety orders。
  - final go/no-go JSON。
- [ ] safety lane 必须对 `cancel/protection/exit/flatten` 保持可用：
  - 允许 recovering/degraded 下执行。
  - 仍要求 exec session ready、idempotency ready、broker conflict gate。
  - 不受 entry data gate 和普通 strategy admission 阻断。
- [ ] recovery flatten/protection 必须有稳定 idempotency key：
  - `runtime_recovery:{strategy_id}:{symbol}:{action}:{broker_qty_sign}`
  - 同一 intent 多轮 reconcile no-op。
  - 不同 sign/action 不得被旧 working recovery order 误吞。
- [ ] broker-only 或 owner 不明确仓位必须 quarantine/manual lock，不得猜最新 working order owner。
- [ ] EOD flatten 必须走 safety lane；数据不可用、策略 seed 不足、普通 health degraded 不能阻断 EOD flatten。
- [ ] 单实例锁必须保持当前语义：不删除锁文件，只 `LOCK_UN + close`。
- [ ] 订单/仓位生产 invariant 必须由周期性 reconciler 守护：
  - 非零 broker position 必须有保护单、flatten plan、quarantine 或 manual lock。
  - broker-only residual owner 不明确时必须 quarantine。
  - strategy memory 不作为恢复 truth。
  - 所有生产下单必须经 UOM。
  - `entry` 可被 paused/recovering 阻塞。
  - `cancel/protection/flatten/close_only` 不被普通 health/data/entry gate 阻塞。
- [x] EOD flatten 必须有平台级 supervisor，不能只依赖策略循环：
  - 每天固定时间扫描 IB positions。
  - 按 strategy recovery policy flatten 或 quarantine。
  - 数据不可用时仍执行。
  - 策略 loop stall 时仍执行。
  - 超过交易时间仍有仓位时 alert + manual lock + emergency workflow。
  - 没有平台级 EOD supervisor 时，禁止 `production_ready=true`。

### P1 - 数据与策略运行门禁

- [x] startup smoke 必须覆盖 no-external strategy dry-load 路径：
  - 使用生产 enabled strategy specs。
  - 调用 `StrategyEngine.load_strategy(...)` 或专用 dry-load API。
  - 复制生产启动中的 config params / extras / strategy id / admission provider wiring。
  - 使用 fake/no-op ctx、fake event bus、fake submit_intent。
  - no-external-services 模式下不能连接 IB 或真实下单。
  - 该 smoke 只证明 enabled specs 能通过 dry-run dependency graph 加载；生产 `RUNNING` 仍必须依赖 live-loaded with real UOM safety lane active。
  - 验证 lifecycle hooks、health subscription、submit path wiring。
  - 验证 registry name、strategy id 唯一性、params schema。
  - 失败时输出 strategy id、strategy name、config hash、异常类型和 traceback 摘要。
- [x] 生产启动必须禁止“0 live strategies loaded 仍继续交易”：
  - enabled live strategy 全部加载失败时，启动应 halt 或进入 explicit non-trading degraded state。
  - 单个 live strategy load 失败时，必须按配置决定 fail-closed 或 degraded，不能只 log 后吞掉。
  - shadow strategy load 失败可以不阻塞真钱交易，但必须进入 readiness snapshot。
- [x] `run_system` summary 必须输出 enabled / loaded / failed strategy counts：
  - `enabled_live_strategy_count`
  - `loaded_live_strategy_count`
  - `failed_live_strategy_count`
  - `loaded_shadow_strategy_count`
  - `strategy_load_failures`
- [ ] `STRATEGIES_LOADED` 必须明确区分：
  - dry-loaded with no-op submit path。
  - live-loaded with real UOM safety lane active。
  - 只有后者可参与 `production_ready=true`。
- [x] `MarketDataFacade.bars_async()` fallback 返回空时，RTH 内必须产出显式 data-unavailable 事件：
  - `symbol`
  - `resolution`
  - `source=current/base`
  - `rows=0`
  - `rth=true/false`
  - `affected_strategy_ids`
- [x] RTH 内核心 symbol 数据不可用时：
  - 阻止 entry。
  - 不阻止 position management。
  - 不阻止 protection repair。
  - 不阻止 flatten/EOD flatten。
- [ ] 为每个 live strategy 增加 `strategy_runtime_ready` snapshot：
  - loop heartbeat age
  - data age
  - stream_valid
  - seed_ready 或明确“该策略不需要 seed”
  - current position qty
  - protection order presence when positioned
  - last successful manage timestamp
- [ ] QQQ `stream_invalid=true` 在 RTH 超过阈值必须进入 strategy degraded/data-unavailable，而不是只写 heartbeat。
- [ ] QQQ `seed_ready=false` 在 first-entry guard 生效期内必须只阻止 entry，不影响持仓管理和平仓。
- [ ] 高频 fallback warning 需要节流和聚合，避免淹没真正错误。
- [ ] ClickHouse driver exception 不得 silent empty；生产路径需要 health gate 或 explicit degraded。
- [ ] 数据恢复后必须清理 data-unavailable gate，并输出恢复证据。
- [x] 建立中心化 `DataAvailabilityService` 或等价平台 contract：
  - 每个 live strategy 声明 required datasets。
  - 每个 dataset 声明 symbol、resolution、TTL、RTH semantics。
  - 输出单一 health/readiness feed。
  - 数据不可用统一阻止 entry。
  - 数据不可用统一不阻止 safety actions。
  - 数据恢复统一清 gate。
  - QQQ 不能是唯一特殊修复路径。
- [x] `DataAvailabilityService` gate 变化必须事件驱动：
  - `mark_unavailable/clear/expire` 输出 `runtime/audit/system-*.jsonl` 结构化证据。
  - gate 变化通过 listener 进入 `RecoveryReadinessService`。
  - `RuntimeReconciler` 收到 `data_availability` 事件后立即复查 health，不等下一轮定时 reconcile。
- [x] `StrategyDataContract` 支持显式 dataset/resolution/warmup contract：
  - `requirements[].dataset`
  - `requirements[].symbols`
  - `requirements[].resolution`
  - `requirements[].ttl_sec`
  - `requirements[].warmup_required`
  - `requirements[].min_rows`
  - `production_blocking=true/false`
- [x] paper 环境不再把 enabled shadow 当成旁路：
  - 当前 paper 目标是让错误真实暴露，所有 enabled strategy 的 data gate 都影响 go/no-go。
  - 当前 enabled stocks kernel / legacy kernel 已从 `shadow/dry_run` 改为 paper-live 配置。
  - `StrategyDataContract` 默认 `production_blocking=true`；只有显式配置才可降级。
- [x] watchdog/opening health 不允许 stale `system_resumed` 假绿：
  - `latest_system_health_transition()` 对 health transition 设置 TTL。
  - 超过 TTL 的 `running/system_resumed` 必须 unhealthy。
  - watchdog 输出 `system_health_transition_stale` failure。
  - opening report 输出 `system_health:stale` blocker。
- [x] stocks kernel fail-closed readiness 进入中央 go/no-go：
  - `required_data_missing` 上报 `DataAvailabilityService` 的 `strategy_tradability` gate。
  - `feature_not_ready/feature_seed_missing` 上报 `strategy_tradability` gate。
  - readiness 恢复后清理中央 data gate。
- [ ] live 与 shadow strategy readiness 分层仅保留给真钱 live 上线前复用：
  - 若未来重新启用 shadow，必须显式 `production_blocking=false` 并在 go/no-go artifact 标注。
  - shadow strategy 升 live 前必须把 `production_blocking=true` 并完成 RTH drill。

### P1 - 订单与仓位真相

- [ ] RuntimeReconciler recovery action 必须有执行证据：
  - action generated
  - UOM accepted/rejected
  - broker order id / quarantine id
  - final resolved/no-op/manual lock
- [ ] 每个 live strategy 必须声明自己的 recovery policy：
  - broker-only long/short 怎么处理
  - ledger-only 怎么处理
  - protection missing 怎么补
  - TP/SL/order orphan 怎么处理
  - owner ambiguous 怎么处理
- [ ] `core.core_position` 与 `position_ledger_current` 必须每日自动检查净额一致。
- [ ] open broker orders 与 `core.core_order` working states 必须每日自动检查一致。
- [ ] 不允许 `REJECTED` 爆量无告警；REJECTED 需要按 reason 聚合并标注是否安全。
- [ ] 保护单缺失检测必须以 broker truth 为准，不能只看本地 pending state。
- [x] 恢复闭环必须有端到端 drill，不只靠 unit：
  - fake IB adapter + fake pg。
  - 走 RuntimeReconciler / UOM / ExecutionService 真实调用链。
  - 覆盖 broker 裸仓、ledger flat、orphan、ghost、policy action、safety lane、close-only guard、最终 resumed。
- [ ] IB restart system drill 必须覆盖真实恢复拓扑：
  - `IBConnectionManager broker_api_ready`
  - `SystemHealthController` health fanout
  - `RuntimeReconciler` immediate clean run
  - `MarketDataService` broken/restored readiness
  - Kafka producer recovery
  - data availability gate
  - restored 前不得 `system_resumed`
  - restored 后必须 `system_resumed`

### P0 - EOD 与平台兜底

- [ ] EOD 后必须自动验证：
  - broker positions = 0。
  - broker open orders = 0。
  - core working orders = 0。
  - ledger/core 净仓 = 0。
  - RuntimeReconciler clean。
- [ ] EOD supervisor 必须有幂等 run id，重复运行不重复下单。

### P1 - 监控、告警、运维

- [ ] 增加生产状态面板，至少包括：
  - system state / startup state。
  - run_system PID / lock holder。
  - IB market/exec/master connected。
  - broker truth age。
  - latest order truth reconcile。
  - latest position truth reconcile。
  - runtime reconcile clean/dirty reason。
  - strategy loaded count vs expected。
  - strategy runtime readiness。
  - data availability per symbol/resolution。
  - open order count。
  - broker position count。
  - active quarantine count。
  - naked position count。
  - protection missing count。
  - pending recovery action count。
  - UOM rejected safety order count。
- [ ] 告警必须覆盖：
  - IB disconnected > 30 秒
  - broker_api_ready 后 reconciler 未在 N 秒内 clean/resume
  - RTH data unavailable > 15 秒
  - strategy heartbeat stale > 10 秒
  - protection missing while positioned
  - EOD flatten not completed by 16:00 ET + grace
  - unexpected working order after EOD
  - unknown broker-only position
- [ ] Telegram/alert 发送失败必须有 fallback log/metric，不能静默。
- [ ] launchd/stdout/stderr 日志要区分历史 ResourceWarning 与当前 fatal。
- [ ] dashboard/alert 以状态机和 invariant 为核心，不以日志条数为核心。

### P2 - 交付卫生

- [ ] 清理或归类当前未提交文件：
  - `Makefile`
  - `reports/qqq_signal_snapshots/...legacy_off...jsonl`
  - `reports/qqq_signal_snapshots/...legacy_on...jsonl`
  - `tests/integration/test_run_system_startup_smoke.py`
- [ ] 给 startup smoke test 明确归属：保留并纳入 CI，或删除。
- [ ] 默认测试命令必须绑定生产运行时解释器：
  - `make test` 使用 `./venv_ml/bin/python -m pytest` 或等价固定解释器。
  - `make test` 依赖或显式包含 `test-startup-smoke`。
  - CI 和本地验收报告必须显示 Python path、Python version、venv path。
- [ ] 文档和配置标明当前是 paper/live 哪个模式，避免误读。
- [ ] 为上线切换保留 single-command rollback。

## 测试方案

### 单元测试

必须覆盖：

- `run_system` lock：
  - A 持锁时 B 启动失败。
  - A 退出后 B 能启动。
  - 空/脏锁文件不阻塞下一次启动。
  - release 不删除锁文件。
- startup gate：
  - IB ready 但 RuntimeReconciler dirty 时 `production_ready=false`。
  - IB ready + RuntimeReconciler clean + strategy data ready 时 `production_ready=true`。
  - strategy seed 不需要的策略不会被 seed gate 误挡。
  - QQQ seed 不足只阻止 entry，不阻止 manage/flatten。
- data gate：
  - `bars_async` current missing + base rows=0 在 RTH 产出 data-unavailable。
  - data-unavailable 阻止 entry。
  - data-unavailable 不阻止 protection/exit/flatten。
- UOM safety lane：
  - recovering/degraded 下 protection/exit/flatten 允许进入 safety lane。
  - exec not ready 时 safety action 被拒绝并有明确原因。
  - idempotency duplicate no-op。
  - 不同 recovery intent 不被旧 working order broad match 误吞。
- owner resolution：
  - 单一非零 ledger owner 可归属。
  - 多 owner working safety order 不归属，进入 quarantine。
  - stale core_position 不参与 owner fallback。

### 集成测试

必须覆盖：

- `RUN_SYSTEM_SMOKE_CHECK=1`：
  - 输出结构化 JSON。
  - 包含 broker/order/position/data/strategy readiness。
  - dirty 状态返回非 0。
- strategy dry-load smoke：
  - 通过真实 `StrategyEngine.load_strategy(...)` 或 dedicated dry-load API 加载所有 enabled live strategies。
  - 不启动外部 IB 下单路径。
  - 不创建真实订单。
  - 任一 live strategy load failure 时返回非 0。
  - loaded live strategy count 必须大于 0。
- zero-strategy guard：
  - 模拟所有 enabled live strategies load failure。
  - 断言 production startup 不得进入 `production_ready=true`。
  - 断言进入 halted 或 explicit non-trading degraded。
- 启动到 running：
  - ExecutionService active。
  - RuntimeReconciler clean。
  - strategy heartbeat fresh。
  - no working orders when flat。
- IB exec disconnect/reconnect：
  - 进入 recovering。
  - 不发布最终 resumed。
  - reconnect 后 RuntimeReconciler clean 才 resumed。
- ClickHouse unavailable：
  - entry gate blocked。
  - existing position manage/flatten path still runs。
  - health/alert 有明确 data-unavailable。
- broker-only residual position：
  - owner 明确时按策略 policy 执行。
  - owner 不明确时 quarantine/manual lock。
  - 不产生重复 flatten。
- recovery e2e drill：
  - IB disconnect 15 分钟后恢复。
  - broker 有裸仓，DB ledger flat。
  - 存在 orphan/ghost order。
  - recovery policy 生成 flatten/protection。
  - UOM safety lane 放行。
  - ExecutionService close-only guard 不误挡。
  - RuntimeReconciler clean 后才 `system_resumed`。
  - 断言全链路没有重复 safety order。
- EOD flatten：
  - 15:59 触发。
  - 数据不可用时仍触发。
  - IB transient disconnect 后恢复继续 flatten 或进入 manual lock。

### RTH 影子验收

真钱无人值守前至少完成 2 个完整 RTH paper/shadow session：

- 开盘前 09:20-09:29 ET：
  - `production_ready=false` 允许，但必须说明原因是 market not open/seed not ready。
  - IB connected。
  - no stale broker-only position。
- 开盘 09:30-09:45 ET：
  - data availability ready。
  - strategy heartbeat fresh。
  - QQQ seed 或 first-entry guard 状态符合配置。
  - no unexpected data-unavailable。
- 盘中：
  - entry blocked/allowed 的 reason 完整。
  - no silent empty data。
  - no orphan working orders。
- 收盘 15:55-16:05 ET：
  - EOD flatten path 证据完整。
  - broker positions flat。
  - open orders 0。
  - reconciler clean。

### 故障演练

真钱无人值守前必须演练：

- IB Gateway 重启。
- IB exec client 断开 2-5 分钟。
- ClickHouse 停止 1 分钟再恢复。
- Kafka consumer assignment timeout。
- run_system 被 SIGTERM 后 launchd 重启。
- PostgreSQL 短暂不可用。
- 人为插入 stale local core_position。
- 人为模拟 broker-only position。

每个演练必须记录：

- 初始状态。
- 故障注入时间。
- health state 转移。
- safety action 是否仍可用。
- 是否恢复到 clean running。
- 是否需要人工介入。
- 是否有重复订单。

每个演练必须产出机器可判定 artifact：

```json
{
  "artifact_type": "production_drill_result",
  "drill_name": "ib_disconnect_recover",
  "started_at_et": "2026-05-04T09:35:00-04:00",
  "ended_at_et": "2026-05-04T09:55:00-04:00",
  "ok": true,
  "expected_transitions": ["RUNNING", "RECOVERING", "BROKER_READY", "RECONCILED", "RUNNING"],
  "actual_transitions": [],
  "order_ids": [],
  "quarantine_ids": [],
  "duplicate_order_count": 0,
  "final_broker_positions": [],
  "final_broker_open_orders": [],
  "final_core_working_orders": 0,
  "final_position_mismatches": 0,
  "final_order_mismatches": 0,
  "failure_reason": ""
}
```

artifact 必须由测试或演练脚本自动生成，不能只靠人工看日志。

## 验收标准清单

### 生产门禁

- [ ] `production_ready=true` 只在以下全部满足时出现：
  - startup state = `RUNNING`。
  - IB market connected。
  - IB exec connected。
  - IB master connected。
  - master truth complete。
  - ExecutionService active。
  - UOM active。
  - RuntimeReconciler clean。
  - order truth clean。
  - position truth clean。
  - enabled live strategy count > 0。
  - loaded live strategy count > 0。
  - failed live strategy count = 0，或失败策略被明确配置为 non-blocking shadow。
  - live strategies 已经通过 real UOM safety lane wiring，不只是 no-op dry-load。
  - 平台级 EOD supervisor active。
  - live strategy readiness clean。
  - required data available。
  - required subscriptions complete，不能只存在部分 active subscription。
  - quarantine/manual lock count = 0。
- [ ] `ALL SERVICES STARTED` 不再被用作上线判断。
- [ ] 启动后 60 秒内必须有 structured readiness snapshot。
- [ ] dirty readiness 返回非 0 或明确 `production_ready=false`。
- [ ] startup smoke 必须走真实 StrategyEngine dry-load 路径。
- [ ] 默认测试命令和 startup smoke 使用同一个 runtime venv。
- [ ] mandatory startup 阶段失败时不能出现 `RUNNING`。
- [ ] 每个 RTH / fault drill 都有 go/no-go JSON artifact。
  - 已落地 runtime recovery drill artifact：`tests/e2e/test_runtime_recovery_drill.py`。

### 数据面

- [ ] RTH 核心 symbol/resolution 无 `rows=0` fallback 连续超过 15 秒。
- [ ] 若发生 data-unavailable，entry 被阻止，flatten/protection 不被阻止。
- [ ] QQQ heartbeat RTH 内 `stream_invalid=false` 或明确 degraded reason。
- [ ] QQQ first-entry seed 状态有明确 reason，不 silent skip。

### 订单面

- [ ] 平仓时最多存在一个有效 recovery flatten intent。
- [ ] 保护单缺失时必须补单、flatten、quarantine 或 manual lock，不能无动作。
- [ ] `core.core_order` working states 与 IB open orders 一致。
- [ ] `REJECTED` 每日有 reason 聚合，无未知爆量。
- [ ] 所有生产下单路径均经过 UOM。
- [ ] safety order rejected 必须进入 alert/metric，不能只写 debug/info。

### 仓位面

- [ ] broker position 与 ledger/core position 净额一致。
- [ ] broker-only unknown owner 进入 quarantine/manual lock。
- [ ] ledger-only position 有明确 repair 或 audit。
- [ ] 非零 broker position 必须存在保护单、flatten plan、quarantine 或 manual lock。
- [ ] EOD 后 broker position = 0，open orders = 0。

### 恢复面

- [ ] IB 重启后不早发 `system_resumed`。
- [ ] RuntimeReconciler clean 后能恢复到 running。
- [ ] dirty reconcile 不会恢复 running。
- [ ] running 后 market-data/bar/tick readiness stale 会进入 degraded/recovering，entry 阻断，safety lane 可用。
- [ ] reconnect/reconcile 不产生重复 flatten/protection。
- [x] IB disconnect 15 分钟恢复 drill 通过。
- [ ] ghost/orphan/mismatch drill 通过。
- [x] close-only safety drill 通过。

### EOD 面

- [ ] 平台级 EOD flatten supervisor 运行成功。
- [ ] 策略 loop 停止时 EOD supervisor 仍能执行。
- [ ] 数据不可用时 EOD flatten 仍能执行。
- [ ] 超时未平仓进入 manual lock + alert。
- [ ] EOD supervisor 是 `production_ready=true` 的必选依赖。

### 运维面

- [ ] 单实例锁验证通过。
- [ ] launchd 自动重启验证通过。
- [ ] alert 通道可用。
- [ ] rollback command 验证通过。
- [ ] 当前 repo 工作区干净，部署 commit 明确。

## Go / No-Go

Go 条件：

- P0 全部完成。
- P1 中数据门禁、safety lane、reconciler、EOD flatten、alert 全部完成。
- 启动状态机 fail-closed 完成。
- 所有 enabled live strategies 通过真实 StrategyEngine dry-load。
- 生产启动不能容忍 loaded live strategy count = 0。
- 恢复/订单闭环 e2e drill 通过。
- 平台级 EOD supervisor 验收通过。
- 所有 RTH/fault drill 产出机器可判定 artifact 且 `ok=true`。
- 至少 2 个 RTH paper/shadow session 通过。
- 故障演练通过。
- 工作区干净，部署 commit 固定。

No-Go 条件：

- 任一 broker/order/position truth 不 clean。
- 任一 live strategy RTH `stream_invalid=true` 且无明确 degraded gate。
- enabled live strategy 未通过真实 load path。
- loaded live strategy count = 0。
- mandatory startup 阶段失败仍继续 running。
- data-unavailable 仍可能 silent empty。
- safety action 仍可能被普通 paused/recovering/degraded/data gate 阻止。
- EOD flatten 无证据。
- EOD 只依赖策略循环，没有平台级兜底。
- drill 没有机器可判定 artifact。
- unknown broker-only position 未 quarantine。
- run_system 不是单实例。
- alert 不可用。

## 最小落地顺序

1. 默认 `make test` 绑定 runtime venv 并包含 startup smoke。
2. StrategyEngine dry-load smoke：定义第一类 DryRunStrategyContext/Noop contracts，用真实 enabled strategy specs 验证 load path。
3. Startup state machine fail-closed：mandatory 阶段失败不能 running。
4. Zero-live-strategy guard：live strategies 全部失败时 halt/degraded，不能 running。
5. 平台级 EOD flatten supervisor，作为 `production_ready=true` 的必选依赖。
6. `ProductionReadinessGate`：把 startup summary 改成生产门禁，不再只看 IB。
7. DataAvailabilityService：`bars_async rows=0` 在 RTH 进入统一 health/strategy gate。
8. DataAvailabilityService listener/audit：gate mark/clear/expire 立即触发 RuntimeReconciler 复查。
9. StrategyDataContract：每个 enabled strategy 显式声明 symbols/datasets/resolution/TTL/warmup；paper 下所有 enabled strategy 默认阻止 go/no-go。
10. Strategy readiness snapshot：QQQ 和 stocks_kernel 至少先接入。
11. Safety action bypass audit：确认 protection/exit/flatten/EOD flatten 不受 data/entry gate 影响。
12. RuntimeReconciler production clean evidence：结构化输出 clean/dirty reason。
13. Recovery e2e drill：fake IB + fake pg 走 Reconciler/UOM/ExecutionService 链路。
14. RTH startup smoke test：自动产出 go/no-go JSON。
15. 故障演练脚本和 artifact 归档。

## 启动验收分层

不要把所有生产证明压进一个 smoke。上线前验收分三层：

### 1. `startup_smoke`

无外部依赖，使用真实配置和真实 enabled strategy specs。

必须验证：

- `run_system` 可 import。
- 配置可解析。
- phase0/UOM/admission provider 可构造。
- RuntimeReconciler / ExecutionService 关键依赖可 dry-wire。
- StrategyEngine real dependency graph dry-wire 使用 `RealEngine` + fake PG/market/pipeline，覆盖非 no-op load wiring。
- `StrategyEngine.load_strategy(...)` 或 dedicated dry-load API 可加载 enabled live strategies。
- loaded live strategy count > 0。
- load failure 有结构化输出。

不得执行：

- 真实 IB 下单。
- 真实 broker order modify/cancel/place。
- 修改真实仓位状态。

### 2. `infra_readiness`

验证外部基础设施，不触发交易。

必须验证：

- PostgreSQL 连接与核心 schema。
- ClickHouse 连接与核心表 freshness。
- Kafka topic / consumer assignment。
- Redis 连接。
- logs / runtime state / lock path 可写。
- alert channel dry-run。

### 3. `broker_readiness`

验证 IB 和 broker truth，不允许下单。

必须验证：

- IB market session ready。
- IB exec session ready。
- master session ready。
- master truth snapshot complete。
- broker positions snapshot 成功。
- open orders snapshot 成功。
- RuntimeReconciler dry run clean。
- 若 dirty，必须输出 ghosts/orphans/mismatches/position_mismatches/quarantine reason。
