# Runtime Lifecycle Root Cause, TODO, Tests, Acceptance - 2026-05-14

## Verdict

这次问题不能再归结为“IB 偶发连不上”或“某个策略 bug”。IB 连接失败、保护单不一致、manual lock、`system_paused` 粘滞、`production_state` transient no-go 都是真实症状，但共同根因是：系统没有一个唯一的 runtime lifecycle controller。

现在有多个控制面同时写入或解释运行状态：

- `IBConnectionManager` 判断 market/exec/master/readiness 和 reconnect。
- `SystemHealthController` 发布 `system_paused` / `system_recovering` / `system_degraded` / `system_resumed`。
- `ProductionStateEmitter` 聚合 watchdog 结果，再反向驱动 system health。
- `TradabilityService` 做 scoped entry 授权。
- `PositionSafetySupervisor` 用 broker truth / core positions / open orders / quarantines / manual locks 判断安全性。
- strategy base class 维护本地 `_system_paused` latch。
- runtime reconciler 可以创建 recovery incident / quarantine / repair action。
- 启动脚本和 launchd 又单独处理 process / lock / interpreter / log。

这些组件各自局部合理，但它们不是同一个状态机的 projection。结果是：一个组件已经恢复，另一个组件仍然保留旧状态；一个组件只想阻止某个策略 entry，另一个组件把它扩大成 platform no-go；一个启动失败看起来像 IB 故障；一个保护单短暂未收敛会升级成 quarantine/manual lock。

所以结论是：大部分反复问题是架构问题，不是单点 bug。里面确实有具体 bug，但这些 bug 反复出现的原因，是架构边界允许它们互相放大。

## Current Evidence

当前运行态本身可以是好的，但这不等于架构已经稳定：

- runtime PID `87193` 正在运行 `run_system.py`，并且到 `127.0.0.1:4002` 有 established IB 连接。
- `runtime/readiness/production_state/production_state_latest.json` 当前为 `go_no_go=go`, `entry_go_no_go=partial`, `safety_go_no_go=go`, `global_halt=false`。
- `runtime/readiness/position_safety/position_safety_latest.json` 当前为 `go`。
- NVDA 两个策略 heartbeat 当前 `tradable=true`, `blockers=[]`。
- NVDA 近期确实发生 flatten、重新 buy filled，并提交 SL/TP。

但代码结构仍暴露以下问题：

- `scripts/run_system_autostart.sh` 固定使用 `venv/bin/python`，并在启动前无条件删除 `runtime/locks/run_system.lock`。这会把 wrong interpreter、stale process、stale lock 混成“IB 没连上”。
- `services/ibkr/ib_connection_manager.py::wait_until_ready()` 在同一个 readiness loop 中串行尝试 market、exec、master；如果 market 正在持有 reconnect gatekeeper，后续 exec/master 可能被 gatekeeper 拒绝并把启动态污染成 degraded。
- `run_system.py` 先写 boot no-go artifact，再等 IB readiness，再启动 safety supervisors 和 production state emitter。这个 boot artifact 是必要的，但如果 artifact freshness / runtime identity 没有被所有读取方严格使用，就可能被旧读者误当成当前 no-go。
- `services/production/production_state_emitter.py::_sync_system_health()` 会把 production state 的 no-go 反向发布到 health controller。也就是说 production state 既是 report，又参与控制。
- `libs/strategy/base.py::on_system_health_event()` 仍有本地 `_system_paused` latch。它不是 scoped decision，容易在恢复后继续影响策略。
- `services/exec/execution_service.py` 已经有 `TradabilityService` second-check，这是正确方向，但它只约束 execution entry，不自动清理 strategy pause latch、manual lock、protective lifecycle、artifact staleness。
- `services/production/position_safety_supervisor.py` 会在 `unsafe_broker_position`、active quarantine、manual lock 等条件下生成 no-go。这里缺的是保护单生命周期中间态，而不是单纯再加一个“忽略错误”。

## Root Cause Breakdown

### RC1 - No Single Runtime Lifecycle Authority

系统现在没有一个权威状态机回答这些问题：

- 当前 runtime 是 booting、waiting for IB、recovering、broker truth syncing、protective verifying、running，还是 manual required？
- 哪些状态允许 strategy entry？
- 哪些状态只允许 close/reduce/protective repair？
- 哪些状态应该 pause strategy loop？
- 哪些状态只是 readiness transient，不能变成 permanent latch？
- 哪个 artifact 属于当前 PID / git sha / config hash？

现在这些答案分散在多个服务中。`go_no_go=go`、`entry_go_no_go=partial`、`system_health=degraded`、`strategy._system_paused=true`、`position_safety=go` 可以同时存在。只要其中一个旧状态没有及时清除，策略就停住。

### RC2 - Startup Is Not Deterministic

启动链还不是单入口、单解释器、单诊断报告。

当前已看到的具体风险：

- `venv` / `venv_ml` 混用，导致依赖缺失时被误判为 IB 或 runtime 故障。
- autostart 脚本无条件删除 lock，可能掩盖真实的 single-instance 状态。
- old process、lock、port、artifact、launchd log 没有一个统一的 preflight/postflight 输出。
- 启动失败后，operator 看到的是一堆 readiness/no-go/IB 日志，而不是明确的“没有用正确入口启动”。

这是架构问题，也包含一个直接 bug：生产启动脚本不能硬编码错误解释器。

### RC3 - IB Readiness And Reconnect Are Still Too Entangled

IB 连接层已经比以前强很多：有 gatekeeper、API probe、short circuit、market/exec/master 分工、request governor。但 readiness 仍然不是严格阶段化。

当前风险：

- `wait_until_ready()` 可以在 market 连接动作未完全释放 gatekeeper 时继续尝试 exec/master。
- readiness 结果是 tuple `(market_ready, exec_ready)`，但真正的生产 readiness 至少还需要 master、broker truth、runtime reconcile、artifact freshness。
- reconnect 成功与 strategy resume 之间没有一个唯一 lifecycle generation 绑定。
- drill artifact、production state、system health 都能表达“恢复不完整”，但不是由同一个 controller 收敛。

这会制造“IB 其实已经能连，但系统仍然 degraded/recovering/paused”的状态。

### RC4 - Protective Order Lifecycle Is Not A Transactional State Machine

保护单问题不是“多加几个 reconcile if”能彻底解决的。

真实交易链路是：

1. entry intent accepted
2. entry submitted
3. entry filled
4. required protection calculated
5. SL/TP submitted to broker
6. broker ack received
7. core order rows persisted
8. ledger owner lane linked
9. broker open orders read back
10. protection verified

当前系统把这些事实分散在 execution service、order manager、broker truth、core order rows、ledger、runtime reconciler、position safety supervisor 中。中间任何一个读到半收敛状态，都可能判断成 `broker_position_without_protection_flatten_quarantine_or_manual_lock`。

这是架构问题。单点 bug 修复只能减少误报，不能保证 entry + protection + broker truth convergence 原子化。

### RC5 - Scoped Tradability Is Correct But Not Sufficient

`TradabilityService` 的方向是正确的：entry should be scope-based, not global. `ExecutionService` second-check 也是必须保留的底线。

但它只解决“某个 intent 是否允许下单”。它不解决：

- strategy loop 是否被 `_system_paused` latch 卡住；
- production state 是否被 stale artifact 污染；
- position safety 是否升级成 global blocker；
- IB reconnect 是否完成；
- protective order 是否 verified；
- operator 是否误读 `entry_go_no_go=partial`。

所以 scoped tradability 是必要条件，不是完整 control plane。

### RC6 - Artifacts Are Reports, But Some Code Treats Them As Control State

readiness artifact 很有价值，但 artifact 必须有强 runtime identity 和 freshness contract。

当前风险：

- boot no-go artifact、drill artifact、watchdog alert state、production state latest 都可能被不同读取方看到。
- 如果读取方不验证 `process_pid`、`process_start_time`、`git_sha`、`generated_at` TTL、runtime lifecycle generation，就可能把旧 artifact 当成当前控制状态。
- stale/no-go transient 触发 health degrade 后，strategy pause latch 可能比 artifact 活得更久。

这类问题必须用 controller generation 和 artifact contract 解决，不能靠人工清文件。

### RC7 - Tests Are Module-Strong But Lifecycle-Weak

已有 unit 覆盖不少：IB reconnect、request governor、scoped tradability、reconciler、production readiness、no-live guard。但反复出问题的是长链路时序：

- IB restart + open positions + live SL/TP + broker truth readback + strategy pause/resume。
- entry filled 后 SL/TP 一部分落库、一部分 broker ack、一部分 readback 延迟。
- stale artifact 被 emitter/readiness 读到后是否会永久 pause。
- process restart 后 owner lane、core orders、ledger、broker truth 是否在同一个 lifecycle generation 下收敛。

这些不是单个 unit test 能证明的。需要 runtime lifecycle E2E 和 chaos regression。

## Architecture Problems vs Bugs

| Category | Item | Type | Why it keeps recurring |
| --- | --- | --- | --- |
| Startup | `venv` / `venv_ml` 混用、入口不唯一 | Bug + architecture | 单个脚本错了是 bug；没有唯一启动入口是架构问题 |
| Runtime lock | lock 被脚本直接删除 | Bug | 破坏 single-instance 语义，掩盖旧进程状态 |
| IB readiness | market/exec/master readiness 不严格阶段化 | Architecture | gatekeeper 是局部互斥，不是 lifecycle authority |
| System health | production state 反向驱动 health | Architecture | report 和 control 混在一起 |
| Strategy pause | `_system_paused` 本地 latch | Architecture + bug risk | 恢复条件不由 scoped decision 统一派生 |
| Tradability | `entry_go_no_go=partial` 易被误读 | Contract gap | 粗粒度状态还在被人工和旧代码读取 |
| Protective orders | entry/protection/broker truth 非事务化 | Architecture | 半收敛窗口会被安全系统放大成 quarantine/manual lock |
| Position safety | no protection -> recovery/manual lock | Correct safety behavior, incomplete lifecycle input | 安全逻辑本身不应弱化，但输入必须表达中间态 |
| Artifacts | stale/latest artifact 可参与控制 | Architecture | 缺 runtime generation / freshness gate |
| Tests | 缺 restart + open order + broker truth chaos | Test architecture | unit 正确不能证明全天运行正确 |

## Target Architecture

新增唯一 `RuntimeLifecycleController`，成为 runtime 状态唯一 authority。其他组件变成输入源或 projection。

Recommended states:

- `BOOTSTRAPPING`
- `STARTUP_PREFLIGHT_FAILED`
- `IB_PORT_WAITING`
- `IB_API_PROBING`
- `IB_CONNECTING_MARKET`
- `IB_CONNECTING_EXEC`
- `IB_CONNECTING_MASTER`
- `BROKER_TRUTH_SYNCING`
- `RUNTIME_RECONCILING`
- `PROTECTIVE_VERIFYING`
- `TRADABILITY_PUBLISHING`
- `RUNNING`
- `RUNNING_PARTIAL_ENTRY`
- `DEGRADED_CLOSE_ONLY`
- `RECOVERING`
- `MANUAL_REQUIRED`
- `HALTED`

Required rules:

- Only `RuntimeLifecycleController` can publish pause/resume/degraded/halted events to strategies.
- `ProductionStateEmitter` writes a projection of lifecycle + tradability + safety, not an independent control decision.
- `SystemHealthController` becomes an event fanout/projection layer, not a separate state authority.
- `TradabilityService` remains the final entry authorization API, but it reads lifecycle snapshot and exact scope.
- `PositionSafetySupervisor` can request lifecycle transitions, but cannot independently create permanent global pause outside the controller.
- Every no-go must include owner, source component, lifecycle generation, clear condition, and whether close/reduce/protective repair is allowed.

## TODO

### P0 - Stop The Daily False-Failure Loop

- [x] Add `scripts/start_paper_runtime.sh`.
  - Fixed interpreter: `venv_ml/bin/python` unless explicitly overridden by a checked env var.
  - Fail fast if interpreter missing `dotenv`, `ib_insync`, database dependencies, or expected repo imports.
  - Preflight checks: existing run_system process, lock owner PID, IB port TCP status, IB API handshake, `.env` trading mode, `IB_ACCOUNT`, config hash.
  - Never delete `runtime/locks/run_system.lock` unless lock owner PID is dead and this is recorded.
  - Postflight checks: runtime PID, `production_state` freshness, IB established connection, lifecycle state, git sha, config hash.
  - Output one machine-readable JSON result and one short human-readable summary.

- [x] Change `scripts/run_system_autostart.sh` to call `scripts/start_paper_runtime.sh` instead of launching `run_system.py` directly.

- [x] Add `scripts/check_paper_runtime.sh`.
  - Reports current PID, Python interpreter, git sha, process start time, IB socket, production state, position safety, strategy tradability, active manual locks, active quarantines.
  - Distinguishes `process_not_started`, `wrong_interpreter`, `ib_port_down`, `ib_api_unhealthy`, `runtime_artifact_stale`, `runtime_degraded`, `entry_partial_expected`.

- [x] Fix `IBConnectionManager.wait_until_ready()` to be staged.
  - Do not attempt exec/master in the same loop tick immediately after a market connect attempt that may still own gatekeeper.
  - Return a structured readiness object: market, exec, master, broker_truth, gatekeeper_state, circuit_state, lifecycle_stage.
  - Unit test gatekeeper active owner behavior.

- [x] Add top-level runtime identity to production artifacts.
  - `runtime_pid`
  - `runtime_process_start_time`
  - `runtime_git_sha`
  - `runtime_config_hash`
  - `runtime_lifecycle_generation`
  - `artifact_source_pid`
  - `artifact_source_component`

- [ ] Make stale artifact reads non-control by default.
  - Stale artifact may alert.
  - Stale artifact may block new entry only if lifecycle cannot prove current state.
  - Stale artifact must not create permanent strategy pause.

- [ ] Remove direct strategy pause authority from local latches.
  - Strategy can cache last event for observability.
  - Entry/exit behavior must derive from current lifecycle snapshot + `TradabilityDecision`.
  - 2026-05-14 partial: base strategy now honors `runtime_lifecycle_state` metadata to clear/set pause, but strategy-local latch still exists and must be fully retired.

### P1 - Build The Runtime Lifecycle Controller

- [x] Add `services/runtime/lifecycle_controller.py`.
  - Owns lifecycle state, generation, runtime identity, transition log.
  - Accepts typed inputs from IB, broker truth, reconciler, position safety, production readiness, operator controls.
  - Emits typed outputs: strategy loop state, allowed flow classes, production projection, alerts.

- [x] Add `RuntimeLifecycleSnapshot`.
  - Includes `state`, `generation`, `entered_at`, `runtime_identity`, `ib_readiness`, `broker_truth_readiness`, `protection_readiness`, `tradability_summary`, `clear_conditions`.

- [ ] Refactor `run_system.py` startup around lifecycle transitions.
  - Boot writes `BOOTSTRAPPING`.
  - IB startup moves through explicit market/exec/master states.
  - Broker truth and runtime reconciler must complete before `RUNNING` or `RUNNING_PARTIAL_ENTRY`.

- [x] Make `ProductionStateEmitter` a projection.
  - It should not independently publish `system_degraded` based only on its own artifact interpretation.
  - It should ask lifecycle controller for desired health projection.

- [x] Make `SystemHealthController` a fanout.
  - Receives lifecycle events.
  - Publishes to subscribers.
  - Does not decide resume blocking from production state failures independently.
  - 2026-05-14 partial caveat: `publish_lifecycle_snapshot()` exists and lifecycle resume bypasses legacy platform-readiness gates; old `publish()` paths still exist for IB/recovery compatibility.

- [ ] Make `TradabilityService` consume lifecycle.
  - `ENTRY` denied unless lifecycle is `RUNNING` or `RUNNING_PARTIAL_ENTRY` and exact scope is tradable.
  - `EXIT`, `SCALE_OUT`, `STOP_LOSS`, `TAKE_PROFIT`, `PROTECTIVE_REPAIR`, `ACCOUNT_EMERGENCY_FLATTEN` remain allowed in degraded close-only states when safe.

### P1 - Make Protective Orders Transactional

- [ ] Add protection lifecycle table or durable artifact with a stable `protection_group_id`.

- [x] Track explicit states:
  - `ENTRY_INTENT_ACCEPTED`
  - `ENTRY_SUBMITTED`
  - `ENTRY_FILLED`
  - `PROTECTION_REQUIRED`
  - `PROTECTION_SUBMITTING`
  - `PROTECTION_BROKER_ACKED`
  - `PROTECTION_CORE_PERSISTED`
  - `PROTECTION_BROKER_TRUTH_SEEN`
  - `PROTECTION_VERIFIED`
  - `ACTIVE_PROTECTED`
  - `EXITING`
  - `FLAT_PENDING_PROTECTION_CANCEL`
  - `CLOSED_VERIFIED`
  - `QUARANTINED_MANUAL_REVIEW`

- [x] Position safety must understand protection intermediate states.
  - Within a short configured protection convergence window, classify as `protection_converging`, not `unsafe_broker_position`.
  - After timeout, escalate to `DEGRADED_CLOSE_ONLY` or `MANUAL_REQUIRED` with exact owner lane.

- [ ] Reconciler must repair by protection group, not just symbol aggregate.
  - Preserve strategy id, strategy instance id, lot id, owner lane, broker order id, perm id, core order id.

- [x] Add idempotent protection verification.
  - Broker open order present.
  - Core order row present.
  - Ledger owner lane present.
  - Quantity and side match.
  - Strategy scope matches.
  - 2026-05-14 partial: existing `core_protection_order_registry` rows now map into `ProtectionLifecycleRecord` without requiring a live DB schema change.

### P2 - Eliminate IB Side Channels

- [ ] Update governance linter to fail production code that creates raw `IB()` or calls expensive IB APIs outside approved adapters/governor.

- [ ] Migrate remaining direct IB call sites in strategy/data/execution fallback code behind `IBConnectionManager` + `IBRequestGovernor`.

- [ ] Add allowlist annotations only for diagnostics/tests, with tests proving no production path uses them.

### P2 - Monitoring And Operator Runbook

- [ ] Split alerts into:
  - true IB outage
  - wrong startup/interpreter
  - stale artifact
  - lifecycle recovering
  - scoped entry partial
  - protective convergence timeout
  - manual intervention required

- [ ] Add a one-command incident bundle:
  - process tree
  - lock state
  - IB socket/probe
  - lifecycle snapshot
  - production state
  - position safety
  - strategy tradability
  - broker truth summary
  - active orders/positions
  - last 500 structured events

- [ ] Update docs/runbook:
  - How to start.
  - How to check.
  - Which artifact is authoritative.
  - What can be cleared.
  - What must never be manually deleted during an active position.

## Test Plan

### Unit Tests

- [x] `tests/unit/runtime/test_lifecycle_controller.py`
  - boot -> IB port waiting -> API probing -> market -> exec -> master -> broker truth -> reconcile -> running.
  - IB disconnect moves to `RECOVERING` and allows close/protective flows only.
  - production scoped blockers create `RUNNING_PARTIAL_ENTRY`, not global halt.
  - hard safety blockers create `DEGRADED_CLOSE_ONLY` or `MANUAL_REQUIRED`.
  - stale artifact cannot produce permanent pause.

- [x] `tests/unit/tool/test_start_paper_runtime_script.py`
  - rejects wrong interpreter.
  - refuses to remove live lock owner.
  - clears dead lock only with evidence.
  - emits JSON diagnostics for missing IB port, API unhealthy, stale production state.

- [x] `tests/unit/services/test_ib_connection_manager.py`
  - staged wait does not call exec/master while market connect owns gatekeeper.
  - half-open API transitions to clear lifecycle stage and alert.
  - successful reconnect emits one lifecycle recovery completion.

- [x] `tests/unit/services/test_production_state_lifecycle_projection.py`
  - projection includes top-level runtime identity.
  - `go + entry partial + safety go` is represented without health degraded.
  - stale boot artifact is ignored for control if PID/generation mismatch.

- [x] `tests/unit/services/test_system_health_controller.py`
  - strategy does not keep `_system_paused` after lifecycle generation resumes.
  - strategy entry denial comes from `TradabilityDecision`, not stale event latch.
  - close/reduce/protective repair still allowed in degraded close-only.

- [x] `tests/unit/exec/test_protective_lifecycle_state_machine.py`
  - entry fill creates `PROTECTION_REQUIRED`.
  - SL/TP broker ack and core persist converge to `PROTECTION_VERIFIED`.
  - duplicate/replayed fills are idempotent.
  - timeout escalates with owner lane and clear condition.

- [ ] `tests/unit/tool/test_check_ib_request_governance.py`
  - production direct `IB()` and direct expensive API calls fail the linter.

### Integration Tests

- [x] `tests/integration/runtime/test_restart_with_open_protection.py`
  - Set up NVDA two strategies with nonzero positions and each strategy's SL/TP.
  - Restart runtime.
  - Assert broker truth, core orders, ledger owner lanes, and protection lifecycle converge.
  - Assert no active quarantine, no manual lock, no stuck `system_paused`.
  - 2026-05-14 partial caveat: implemented as a deterministic integration-level supervisor/restart invariant, not a real process restart against live IB.

- [ ] `tests/integration/runtime/test_ib_restart_lifecycle.py`
  - Simulate IB port refused, API unhealthy, then ready.
  - Assert lifecycle transitions are ordered and single-generation.
  - Assert no launchd/process restart loop is required.

- [ ] `tests/integration/runtime/test_production_state_partial_entry.py`
  - Some option strategies blocked by session/data.
  - NVDA strategies tradable.
  - Assert platform safety remains go and NVDA entry is allowed.
  - Assert monitoring reports partial entry, not global no-go.

- [ ] `tests/integration/runtime/test_protection_convergence_window.py`
  - Entry filled.
  - Broker ack delayed.
  - Core persist delayed.
  - Position safety sees `protection_converging` during window.
  - After convergence, state becomes protected.
  - After timeout, exact owner lane escalates.

- [ ] `tests/integration/runtime/test_stale_artifact_does_not_pause.py`
  - Write stale boot no-go / stale drill artifact from old PID.
  - Start current lifecycle as running.
  - Assert stale artifact alerts but does not pause strategies.

### E2E / Chaos Tests

- [ ] Controlled paper runtime restart while holding protected NVDA position.
  - Expected: runtime returns to `RUNNING` or `RUNNING_PARTIAL_ENTRY`.
  - Expected: SL/TP still verified.
  - Expected: zero manual locks and zero active quarantines.

- [ ] IB Gateway disconnect / externally initiated restart during market hours.
  - Constraint: agents and scripts must not kill, restart, force-quit, or reopen IB Gateway. In this environment a Gateway restart requires manual password entry and is not automatically recoverable.
  - Expected: lifecycle moves to `RECOVERING` / `MANUAL_REQUIRED` without request storms.
  - Expected: new entries denied.
  - Expected: exits/protective repair allowed only when broker channel is safe.
  - Expected: after operator restores IB API readiness and authorized proof succeeds, lifecycle resumes without manual file deletion.

- [ ] Half-open IB API.
  - TCP port open, API handshake fails.
  - Expected: `IB_API_UNHEALTHY`, no request storm, no strategy entry.

- [ ] Wrong startup path.
  - Direct `venv/bin/python run_system.py` should be detected and rejected by wrapper/checker as unsupported for production.

- [ ] Stale artifact replay.
  - Reintroduce old `production_state_latest.json` and drill artifact.
  - Expected: rejected by generation/freshness checks.

- [ ] Multi-hour soak.
  - Run through one full RTH session in paper.
  - Expected: no stuck pause, no active manual lock, no quarantine, no duplicate client id, no repeated IB connection storm, all entries audited by scoped tradability.

## Acceptance Criteria

This work is not accepted just because today's `production_state` says `go`. It is accepted only when these conditions hold.

### Startup Acceptance

- [ ] There is exactly one supported paper runtime startup command: `scripts/start_paper_runtime.sh`.
- [ ] Autostart/launchd path uses the same command.
- [ ] The command fails before launching if the interpreter is wrong or dependencies are missing.
- [ ] The command refuses to delete a live lock and reports owner PID.
- [ ] Within 120 seconds of IB API readiness, runtime reaches `RUNNING` or `RUNNING_PARTIAL_ENTRY`.
- [ ] Startup summary includes PID, interpreter, git sha, config hash, IB host/port, IB API readiness, production state freshness.

### IB Recovery Acceptance

- [ ] IB disconnect always moves lifecycle to `RECOVERING` or `DEGRADED_CLOSE_ONLY`.
- [ ] New entries are blocked during unsafe broker states.
- [ ] Close/reduce/protective repair policies are explicit.
- [ ] After IB API readiness returns, market/exec/master/broker truth converge in one lifecycle generation.
- [ ] No recovery path requires deleting lock files or stale artifacts manually.
- [ ] Gatekeeper-denied retries do not create false degraded state during normal staged startup.

### State Authority Acceptance

- [ ] Only `RuntimeLifecycleController` can publish strategy pause/resume/degraded/halted control events.
- [ ] `ProductionStateEmitter` is a projection, not an independent control authority.
- [ ] `SystemHealthController` does not independently block resume from production state artifacts.
- [ ] Strategy entry uses `TradabilityDecision` exact scope.
- [ ] `entry_go_no_go=partial` is never interpreted as platform no-go.
- [ ] Every no-go includes source, owner, scope, lifecycle generation, clear condition, and allowed flow classes.

### Protective Order Acceptance

- [ ] Every filled entry has a durable `protection_group_id`.
- [ ] Protection lifecycle reaches `PROTECTION_VERIFIED` before the position is classified as fully protected.
- [ ] Position safety recognizes configured convergence windows.
- [ ] A restart with open protected positions does not create manual locks or quarantines when broker/core/ledger facts are consistent.
- [ ] If protection cannot be verified after timeout, escalation is owner-scoped and close-only/manual-required, not an ambiguous global pause.

### Artifact Acceptance

- [ ] Production state and safety artifacts include top-level runtime identity.
- [ ] Readers reject stale PID/generation artifacts for control decisions.
- [ ] Stale artifacts can alert but cannot permanently pause strategies.
- [ ] Drill artifacts distinguish last drill result from current runtime readiness.

### Test Acceptance

- [ ] Unit lifecycle tests pass.
- [ ] Integration restart-with-open-protection test passes.
- [ ] IB disconnect / external-restart lifecycle test passes without agent/script-initiated Gateway restart.
- [ ] Stale artifact non-control test passes.
- [ ] IB request governance linter passes.
- [ ] Existing execution, scoped tradability, reconciler, and position safety tests pass.
- [ ] Paper soak passes for 3 consecutive trading sessions, or at minimum 18 RTH hours, with:
  - zero stuck `system_paused`
  - zero unexpected manual locks
  - zero active quarantines
  - zero duplicate client id failures
  - zero IB request storms
  - all entries carrying `TradabilityDecision`
  - all nonzero positions protected or explicitly close-only/recovery-managed

## Implementation Order

1. Build `scripts/start_paper_runtime.sh` and checker first. This removes fake failures and gives a reliable way to operate the current system.
2. Fix staged IB readiness next. This prevents startup from self-degrading.
3. Introduce `RuntimeLifecycleController` as a thin authority around existing components. Do not rewrite every service first.
4. Convert production state and system health to lifecycle projections.
5. Remove strategy local pause authority.
6. Add protection lifecycle states and restart-with-open-protection tests.
7. Tighten IB side-channel linter.
8. Run soak and only then consider the issue closed.

## Non-Goals

- Do not weaken position safety to make dashboards green.
- Do not treat `entry_go_no_go=partial` as a failure by itself.
- Do not solve this by clearing runtime files during active positions.
- Do not add more ad hoc resume scripts that bypass lifecycle.
- Do not accept a one-time successful restart as proof of production stability.

## Definition Of Done

The system is done when a normal operator can run one command, see one lifecycle state, and trust that:

- IB disconnected means entries stop and recovery begins.
- IB recovered means lifecycle resumes exactly once.
- Strategy entry decisions are scoped.
- Protective orders have a durable lifecycle.
- Stale artifacts cannot control current runtime.
- Manual lock/quarantine means a real unresolved safety condition, not a transient startup/readback race.
