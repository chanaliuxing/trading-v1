# Fast Track TODO - 2026-04-21

This list is the execution version of the 5-day fast-track plan, corrected against the current codebase state.

## Already Landed / Verification Required

- [x] T1 non-OCO protection registry writes.
  - Evidence: `UnifiedOrderManager.ensure_protection()` writes `core.core_protection_order_registry`.
  - Evidence: stop and take-profit protection stress test triggers 100 duplicate intents with only 1 delegate call.
- [x] T2 UOM gate lift.
  - Evidence: `HealthGate`, `StrategyAdmissionGate`, `BrokerTruthGate`, `FlipGuardGate`, `PendingOrderGate`, `LedgerTruthGate`, and duplicate protection gate are in `services/orders/manager.py`.
  - Downstream `ExecutionService` gates remain as defense in depth.
- [x] T3 lot-level PositionReconciler visibility.
  - Evidence: ledger query groups by `symbol, strategy_id, lot_id`, then rolls up to symbol for IB comparison.
  - Evidence: reconcile metadata includes `ledger_lots` and `ledger_lot_count_by_symbol`.
- [x] Phase0 freeze gate.
  - Evidence: startup compares current live/shadow surface to `reports/strategy_surface/live_strategy_surface_freeze_baseline.json`.
  - Evidence: UOM runtime admission only allows strategies in the live baseline.

## Active Work

- [x] T4 QQQ D+3 shadow drift report builder.
  - Tool: `tool/report/build_qqq_shadow_drift_report.py`.
  - Required output: days observed, signal snapshot count, parity report count, unresolved diff list, whitelist status, recommendation.
  - Fail-closed semantics: no parity reports -> `HOLD/no_parity_report_yet`; empty legacy/kernel input -> `HOLD/empty_parity_report_input`; D+3 readiness uses `min_match_ratio`.
- [x] T5 QQQ parity trace input verification automation.
  - Pre-open requirement: confirm `qqq_intraday_001`, `qqq_kernel_shadow_legacy_on_001`, and `qqq_kernel_shadow_legacy_off_001` each write non-empty independent JSONL trace files.
  - Required output: legacy trace path, legacy_on trace path, legacy_off trace path, record counts, and first/last timestamps.
  - Blocker: do not schedule or count `run_qqq_kernel_shadow_parity.py` as evidence until both `--legacy` and `--kernel` inputs exist and are non-empty.
  - Smoke test: if a trace exists, run self-parity with the same file as `--legacy` and `--kernel`; expected `diff_total=0`.
  - Evidence: `tool/ops/run_qqq_shadow_preopen_checks.py` refreshes prior-day postclose evidence and runs parity smoke before the open.
  - Evidence: `tool/ops/check_qqq_shadow_trace_coverage.py` fail-closes when any of the three expected trace writers is missing.

## Automation Orchestrator

- [x] Persist current rollout priority task in a fixed state file.
  - Evidence: `runtime/state/trading_rollout_orchestrator_state.json` is built by `tool/ops/update_rollout_orchestrator_state.py`.
- [x] Escalate repeated blockers automatically.
  - Evidence: the orchestrator state tracks blocker streaks and sets `escalation_required=true` when the same blocker appears in consecutive runs.
- [x] Persist automated execution outcomes.
  - Evidence: `tool/ops/record_rollout_execution.py` appends execution results, attempt counts, and last completed task into `runtime/state/trading_rollout_orchestrator_state.json`.

## P1 Incident Hardening

- [x] P1-3 Kafka ClickHouse writer consumer self-healing.
  - Evidence: `services/market/writer/ch_writer.py` uses a single `_restart_consumer()` path for assignment-loss watchdog, commit-time `No partitions assigned`, and retriable consume-loop errors including heartbeat/coordinator/session-expired patterns.
  - Evidence: `tests/unit/services/test_ch_writer_watchdog.py` covers commit-triggered restart, assignment-loss restart after threshold, assignment recovery state clear, and heartbeat/coordinator retriable classification.
  - Runtime acceptance: no repeated `Bar data fallback failed` or Kafka assignment-loss loop after restart.
- [x] P1-1 OrderTruthReconciler stable identity and session isolation.
  - Evidence: `services/reconciliation/contracts.py` matches IB orders by stable `ib_perm_id` first and only uses `broker_order_id` inside the current session window.
  - Evidence: `tests/unit/reconciliation/test_reconciler_contracts.py` covers broker order ID reuse without producing false orphan/ghost matches.
- [x] P1-2 Position ledger to core position synchronization.
  - Evidence: `services/exec/safety_kernel.py` writes fill events to `position_ledger_event`, then syncs `core.core_position` from `position_ledger_current`.
  - Evidence: `tests/unit/exec/test_safety_kernel_position_accounting.py` covers ledger-backed core position sync and legacy fallback.
- [x] Shadow/dry-run bootstrap protection guard.
  - Evidence: `services/exec/safety_kernel.py` skips automatic OCO attach for strategies configured with `shadow_mode`, `dry_run`, or `shadow_only`.
  - Evidence: `tests/unit/exec/test_safety_kernel_oco_persistence.py` verifies normal strategies still reattach protection while `stocks_kernel_shadow_smh_001` does not place real protection orders.
  - Runtime evidence: startup logged `safety_kernel_bootstrap_skip_shadow_dry_run_protection` for `stocks_kernel_shadow_smh_001/SOXS` and `attach_requested=0`.

## P2 Follow-Up Cleanup

- [x] P2-1 non-OCO protective stop registry upsert.
  - Evidence: `services/exec/execution_service.py` prepares protection metadata before persisting `core.core_order` and upserts `core.core_protection_order_registry` after broker submit persistence succeeds.
  - Evidence: `services/exec/safety_kernel.py` provides idempotent non-OCO protection metadata and registry helpers shared with the existing OCO registry writer.
  - Evidence: `tests/unit/exec/test_qqq_v1_trade_safety_regression.py::test_protective_stop_cancels_only_existing_stop_orders` now asserts protective stop registry writes.
- [x] P2-2 shadow strategy config guard.
  - Evidence: `config.yml` already has `shadow_mode: true` for `stocks_kernel_shadow_smh_001` and `stocks_kernel_shadow_spy_001`.
  - Evidence: both shadow strategies also carry `extras.dry_run: true` and `extras.shadow_only: true`.
- [x] P2-3 PositionReconciler lot-level grouping.
  - Evidence: `services/reconciliation/contracts.py` groups `position_ledger_current` by `symbol, strategy_id, lot_id`.
  - Evidence: reconcile reports include `ledger_lots` and `ledger_lot_count_by_symbol`.

## Paper Validation Gates

- [ ] Run paper for 3 complete trading sessions with UOM enabled.
- [ ] Daily 17:30 ET review of QQQ shadow drift report.
- [ ] Protection registry clean: no repeated effective stop/target submits for the same lot and price band.
- [ ] Position ledger clean: IB symbol quantity equals ledger rollup; lot breakdown present in report metadata.
- [ ] Runtime health clean: no `system_health_gate_blocked:recovering` after final `system_resumed`.

## Live Cutover Gates

- [ ] QQQ D+3 drift report has 3 trading days observed.
- [ ] `unresolved_diff_count == 0`.
- [ ] Approved whitelist count `<= 5`.
- [ ] No Phase0 live-surface drift.
- [ ] Manual operator coverage confirmed for first live small-size session.
