# IBKR 落地 TODO / 测试 / 验收清单

## A. TODO（上线前必做）
- 执行 DB migration：`db/pg/migration_20260417_ib_order_truth_and_quarantine.ddl`。
- 在目标环境配置 `ib.registry.*`，确认生产/开发命名空间隔离。
- 校验 `reconciliation.mode=report_only` 生效，禁止任何自动清理任务并行运行。
- 为 quarantine 建立运维闭环（人工 adopt/cancel runbook）。

## B. 测试清单
## B0. 固定回归入口（每次改 IB 连接/恢复必跑）

快速事故回归：
```bash
scripts/run_ib_reconnect_regression.sh fast
```

完整 IB 连接恢复回归：
```bash
scripts/run_ib_reconnect_regression.sh full
```

约束：
- 该脚本默认 `TRADING_TEST_ALLOW_LIVE_IBKR=0`，不得连接真实 TWS/Gateway。
- 真实 IB restart drill 必须独立执行，并显式 opt-in，不能混进 unit/integration 默认回归。
- 任何修改 `services/ibkr/*`, `services/exec/adapters/ib_adapter.py`, `run_system.py`, recovery readiness/system health 时，至少跑 `fast`；合并前跑 `full`。

覆盖目标：
- Gateway circuit open 不能永久阻止恢复 proof。
- 非 manual circuit open 只允许唯一跨进程 recovery probe lease。
- manual required 仍硬阻断自动恢复，但必须显式暴露 `manual_recovery_required`，不能伪装成普通 `startup_ib_not_ready_retrying`。
- 计划内 IB restart 期间，Gateway 端口关闭导致的 `ConnectionRefusedError` 不得在 grace 窗口内升级成 `manual_required`。
- `manual_required=true` 后，必须存在一个 operator ack + 单 lease recovery proof 的闭环；proof 成功后才能清 circuit。
- pre-connect API probe 失败不得重复计数，不得把一次失败放大成 10 分钟卡死。
- gatekeeper denial 不得继续真实连接，不得 `report_failure`。
- market/exec/master startup gate 与 runtime recovery 使用同一个 readiness contract。
- request governance 防止诊断/恢复路径放大成连接风暴。
- runtime process start 早于 IB recovery 关键模块 mtime/git hash 时，production state 必须 `no_go` 并暴露 runtime freshness blocker。
- unit tests 的 no-live IBKR 边界必须有效。

## B1. 已回归通过（本次）
- `tests/unit/services/market/test_ib_market_data_premarket_ports.py`
- `tests/unit/services/test_runtime_reconciler_ghost_cleanup.py`
- `tests/unit/services/test_runtime_reconciler_polling.py`
- `tests/unit/services/test_runtime_reconciler_decision_finalize.py`
- `tests/unit/services/test_ib_connection_manager.py`
  - `test_connection_refused_probe_does_not_mark_manual_required`
  - `test_manual_required_refusal_latch_allows_authorized_recovery_probe`
  - `test_manual_required_half_open_still_blocks_recovery_probe`
- `tests/unit/services/exec/test_ib_adapter_premarket_reconnect.py`
- `tests/unit/exec/test_execution_service_manual_lock.py`

## B1.1. P0-1 Manual-Required Recovery Closure 覆盖

已实现并纳入 `scripts/run_ib_reconnect_regression.sh fast`：
- `test_planned_restart_connection_refused_within_grace_does_not_mark_manual_required`
- `test_connection_refused_outside_grace_enters_recoverable_non_manual_circuit`
- `test_tcp_open_handshake_timeout_can_mark_gateway_api_half_open`
- `test_manual_required_visible_as_manual_recovery_required_not_generic_startup_retry`
- `test_authorized_operator_proof_clears_manual_required_after_time_positions_orders`
- `test_manual_proof_requires_operator_ack_when_manual_required`
- `test_manual_proof_concurrent_callers_only_one_constructs_ib`
- `test_manual_proof_unauthorized_source_does_not_construct_ib`
- `test_manual_proof_failure_uses_report_aborted_not_report_failure`
- `test_manual_required_half_open_still_blocks_recovery_probe`
- `test_runtime_freshness_stale_keeps_production_no_go_for_ib_recovery_modules`

状态机验收：
- Planned restart refusal: declared restart grace 内 `ConnectionRefusedError` -> `RECOVERABLE_RESTARTING`, `manual_required=false`.
- Recoverable non-manual refusal: grace 外 `ConnectionRefusedError` -> `RECOVERABLE_REFUSED`, `manual_required=false`.
- API half-open: TCP/API path可达但 handshake/current-time 超时 -> `MANUAL_REQUIRED`.
- Manual proof closure: operator ack + authorized source + durable lease + current time + positions + open orders 成功后才 close circuit.
- Unauthorized/lease-denied proof: 不构造 `IB()`，不触碰真实连接。
- Proof failure: 保持 manual latch，使用 `report_aborted`，不双计数。

Operator proof API:
```python
await run_authorized_gateway_recovery_proof(
    source="operator:manual_proof",
    ack_token="<operator-ack>",
    now=now,
    host="127.0.0.1",
    port=4002,
)
```

生产运行前提：
- `operator:manual_proof` 或等价 source 必须配置到 `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES`。
- 单元/回归测试必须使用 fake IB；默认不得连接真实 `127.0.0.1:4001/4002`。
- 本清单不等同 live drill。真实 IB Gateway restart drill 必须另行显式执行。

## B1.1 Runtime Operator Proof CLI Acceptance

Implemented path:
- CLI: `scripts/operator_ib_gateway_recovery_proof.py`
- Runtime artifact: `runtime/recovery/ib_gateway_operator_proof/latest.json`
- Operator runbook: `docs/runbooks/ib_gateway_operator_recovery_proof.md`

CLI safety contract:
- Default mode is dry-run and must not construct `IB()`.
- `--live` alone is insufficient; `TRADING_TEST_ALLOW_LIVE_IBKR=1` is also required.
- Manual-required proof requires exact ack text: `I understand this will perform a disposable IB API proof`.
- Source must be listed in `IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES`.
- Durable proof lease must be acquired before IB construction.
- Proof success requires current time, positions, and open orders.
- Proof failure keeps `manual_required=true`.
- Success clears `manual_required` only through the recovery coordinator.
- Readiness exposes latest proof result for observability only; it cannot bypass broker truth, position safety, or runtime freshness gates.

Required tests:
- `test_operator_proof_cli_dry_run_does_not_construct_ib`
- `test_operator_proof_cli_requires_live_flag`
- `test_operator_proof_cli_requires_ack`
- `test_operator_proof_cli_requires_authorized_source`
- `test_operator_proof_cli_lease_denied_does_not_construct_ib`
- `test_operator_proof_cli_success_requires_time_positions_open_orders`
- `test_operator_proof_cli_failure_does_not_clear_manual_required`
- `test_operator_proof_cli_success_clears_manual_required`
- `test_operator_proof_cli_writes_latest_artifact`
- `test_operator_proof_cli_outputs_json`
- `test_operator_proof_cli_exit_codes`
- `test_operator_proof_visibility_in_production_state`

Regression commands:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 venv/bin/python -m pytest -q tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py
scripts/run_ib_reconnect_regression.sh fast
scripts/run_ib_reconnect_regression.sh full
```

Live drill remains explicit opt-in only and is not part of unit/full regression defaults.

## B1.2 Live Operator Drill Prep Only

Preparation artifacts:
- `docs/runbooks/live_ib_gateway_operator_drill_plan.md`
- `docs/reports/templates/live_ib_gateway_operator_drill_evidence_template.md`
- `scripts/check_ib_operator_proof_dry_run.sh`

Acceptance:
- Live drill is manual operator-only.
- Live drill is not included in unit, integration, fast, or full regression defaults.
- Prep does not run `TRADING_TEST_ALLOW_LIVE_IBKR=1`.
- Prep does not connect to real IBKR.
- Prep does not clear a real production circuit.
- Dry-run smoke must return `DRY_RUN` and proof fields must remain `not_run`.

Prep verification command:

```bash
TRADING_TEST_ALLOW_LIVE_IBKR=0 scripts/check_ib_operator_proof_dry_run.sh
```

执行命令：
```bash
venv/bin/python -m pytest -q \
  tests/unit/services/market/test_ib_market_data_premarket_ports.py \
  tests/unit/services/test_runtime_reconciler_ghost_cleanup.py \
  tests/unit/services/test_runtime_reconciler_polling.py \
  tests/unit/services/test_runtime_reconciler_decision_finalize.py \
  tests/unit/services/test_ib_connection_manager.py \
  tests/unit/services/exec/test_ib_adapter_premarket_reconnect.py \
  tests/unit/exec/test_execution_service_manual_lock.py
```

## B2. 待上线前联调（必须）
- 断连注入：重启/杀掉 IBG 后验证 market/exec/master 的 epoch 递增与自动重订阅。
- manual latch 注入：让 circuit 进入 `manual_required=true`，再启动/恢复 IBG，验证系统不会沉默卡住，必须输出 `manual_recovery_required`，且 operator proof 成功后恢复。
- 计划重启注入：在 declared restart window 内关闭 4002 端口，验证 refusal 进入 recoverable restart state，不在 grace 内升级 manual-required。
- Quarantine 闭环：注入未知 broker 单，验证 symbol 新单被拦截。
- 启动闸门：模拟 master truth 不完整，验证交易闸门保持关闭。

## C. 验收门槛
- 指标 1：`clientId` 不漂移。
  验收：同环境重启 10 次，角色 clientId 映射稳定；无随机 +1/+rand 漂移日志。
- 指标 2：无自动 fresh-probe/owner-cancel 辅助会话。
  验收：默认配置下不出现辅助 probe session 建连事件。
- 指标 3：`runtime reconciler` 不自动 terminalize/cancel。
  验收：reconciler 周期内无自动 `UPDATE core.core_order` 终态化动作、无自动 broker cancel。
- 指标 4：IB Gateway restart 后可稳定收敛。
  验收：circuit open 期间无真实连接风暴；authorized recovery probe 成功后 circuit 立即关闭；随后 market/exec/master/master_truth 全部 ready。
- 指标 5：旧事故不回归。
  验收：`scripts/run_ib_reconnect_regression.sh fast` 和 `full` 均通过；日志/测试证明 pre-connect timeout 走 `report_aborted` 而不是重复 `report_failure`。
- 指标 6：manual-required 不永久锁死。
  验收：`manual_required=true` 时生产态明确给出人工恢复动作；IBG 已恢复后，单次授权 proof 能完成 broker time/positions/open orders 检查并关闭 circuit。
- 指标 7：计划内重启不误判为半开。
  验收：declared restart window 内的 `ConnectionRefusedError` 在 grace 内不触发 `gateway_api_half_open/manual_gateway_api_recovery_required`。

## D. 上线后 7 天观察
- 重点看板：
  - reconnect 失败分布（含 in_use/timeout）
  - quarantine 新增速率与未解决时长
  - trading gate 拦截原因分布（ready vs quarantine）
  - Gateway circuit open/close 次数、open duration、recovery probe lease denied/allowed 次数
  - `manual_required` 持续时长、operator ack 等待时长、proof 成功/失败次数
  - declared restart window 内 refusal 次数与是否越过 grace
  - `ib_wait_until_ready_gateway_circuit_cleared_by_probe` 出现后到 `system_resumed` 的耗时
- 退出条件：
  - 7 天内不出现“断线后重连导致 clientId 漂移”事件。
  - 不出现 reconciler 自动误杀订单事件。
  - 不出现 Gateway 已恢复但系统因 stale circuit 卡住超过 60 秒的事件。
  - 不出现 Gateway 已恢复但系统因 `manual_required` 缺少 operator proof 闭环而卡住的事件。
