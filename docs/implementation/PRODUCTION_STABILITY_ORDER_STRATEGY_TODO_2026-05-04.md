# Production Stability, Order Management, and Pluggable Strategy TODO

Date: 2026-05-04

Status: merged into the canonical production release gate at
`docs/implementation/PRODUCTION_GRADE_STOCK_OPTION_PLATFORM_TODO_TEST_ACCEPTANCE_2026-05-05.md`.

Keep this file as historical implementation detail only. New TODO/status updates should go into the
2026-05-05 canonical document so the platform has one release checklist.

Scope: unattended paper/live production readiness after IB restart hardening. This document focuses on whether the current architecture can safely support stable IB sync, clean order management, pluggable strategies, option contract selection, protective orders, EOD flatten, and machine-checkable operations.

## Executive Summary

The architecture has moved in the right direction:

- IB connection recovery is no longer treated as sufficient for system recovery.
- Runtime reconciliation now participates in broker/order/position truth.
- UOM has health gating, safety lane, broker truth gate, quarantine gate, ledger gate, duplicate protection detection, and recovery duplicate detection.
- ExecutionService persists DB state before broker submission and uses idempotency keys.
- Platform EOD supervisor exists and is no longer solely strategy-loop dependent.
- Strategy data contracts and central data availability gates exist.
- Enabled paper strategies are treated as production-blocking. Shadow-style admission is not allowed.

However, it is not yet unattended live production grade. The remaining risks are mostly architecture-boundary issues rather than a single IB reconnect bug:

- Some production safety invariants are still distributed across strategy-local code, UOM, RuntimeReconciler, EOD supervisor, and watchdogs.
- New strategies can be loaded without a first-class production manifest proving data, contract, recovery, protection, EOD, and tradability contracts.
- Stock plugins are still too thin as a production interface: the plugin can define behavior, but the safety/data/recovery/EOD/tradability contract can be scattered across hand-written YAML.
- EOD flatten can submit once and then stop retrying before flat is verified.
- Legacy option execution can bypass UOM if enabled.
- Protection repair is not yet a platform-wide broker-truth invariant.
- Operations go/no-go can still rely on stale or incomplete health artifacts unless watchdog checks are tightened.

2026-05-05 critical additions:

- P0 Broker truth recovery hardening from 20:29 ET review:
  unknown broker fills without an existing unique `core_order` identity must stay in
  `core_order_quarantine` and must not create adopted FILLED orders or PnL ledger events;
  close-only recovery lanes must use broker position truth over DB hints and escalate to
  manual-required when broker truth is unavailable; position safety must submit a bounded
  close-only recovery flatten before falling back to manual-required locks.
  Landed first slice: strict unknown-fill quarantine, broker-truth close-only safety lane,
  position-safety recovery flatten submission, and file-backed recovery incidents that move
  through detected/recovery_submitted/manual_required_acknowledged/verified_flat.
- P1 Stock plugin manifest: `BaseStockPlugin` must support a first-class `StockStrategyManifest`.
  New stock strategies should be admitted through `plugin.production_manifest() -> generated
  stocks_kernel config -> validation -> dry-wire/paper run`, not by manually assembling every
  production contract in YAML. Hand-written config remains valid only when it passes the same
  generated-manifest validation.
- P1 Lifecycle supervisor: a live `run_system.py` process that keeps emitting `no_go` because market data, runtime reconciliation, position safety, or system health artifacts are stopped/stale must not run forever. After a bounded number of consecutive critical `no_go` heartbeats, the process must hard-exit so the external supervisor can restart it, or explicitly enter a manual halt workflow.
- P1 Cleanup truth convergence: residual cleanup must synchronize `core_order`, reservations, `position_ledger_event`, manual intervention locks, and `core_protection_order_registry` in one broker-verified flow. Registry rows must be terminalized by broker id/order ref as well as by strategy/symbol.
- P1 Ledger/account invariants: production reconciliation must report account-scoped ledger divergence, `UNKNOWN_ACCOUNT` lots, and null-lot adjustments as explicit blockers until an adjustment or ownership migration resolves them.
- P1 Strategy tradability proof: production-blocking strategies start as `unknown/no_go` until a fresh persisted readiness/tradability heartbeat proves data warmup, signal loop, and order admission are alive.
- P1 Option handoff platformization: option strategies must consume durable stock trigger events and emit a terminal verdict per trigger. The old latest-signal cache fallback is not allowed in production.
- P1 Contract selection verdicts: every `no_contract_selection` must produce a machine-readable platform artifact with strategy id, symbol, trigger id, and selector reason/diagnostics.
- P0 Stocks-kernel order lifecycle must use real DB schema. `core.core_order` does not have
  `decision_id`; lifecycle reconciliation must use `order_ref`, `broker_order_id`, `ib_perm_id`,
  `note` metadata, and `core.core_order_event.decision_id`, with an integration test against the
  real Postgres schema.
- P1 Ledger integrity must flag offsetting lot/null-lot/account residues even when symbol net
  quantity is flat. Net-flat accounting can still corrupt ownership and price anchors.
- P1 IB restart drill evidence must be bound to the current process. A drill artifact generated
  before the current `run_system.py` process start cannot prove this process passed recovery.
- P1 Stocks-kernel startup blockers must be recoverable. `broker_live_position_present`,
  `broker_open_orders_present`, and DB residual blockers must periodically re-check broker/DB truth
  and clear automatically once truth is clean.

Target production rule:

```text
Production-ready = broker truth clean
                + order truth clean
                + position ledger clean or safely quarantined
                + data/tradability gates clean
                + EOD supervisor active
                + every nonzero broker position is protected, flattening, quarantined, or manually locked
                + every enabled strategy has an explicit production manifest
                + all order paths go through UOM/ExecutionService
                + health/readiness artifacts are fresh and machine-checkable
```

## P0: Must Land Before Unattended Live

### P0.1 EOD Flatten Must Retry Until Verified Flat or Escalated

Problem:

- `PlatformEODSupervisor.run_once()` marks the day as already run before `execute_flatten_all()` finishes.
- If flatten submission fails, only partially submits, or returns `SUBMITTED_NOT_VERIFIED`, the supervisor will not retry later that day.
- `ManualFlattenService` retry watchdog defaults to disabled.

Files:

- `services/production/eod_supervisor.py`
- `services/telegram/manual_flatten_service.py`

Required changes:

- Move `_last_run_day = day` to the verified completion path only. If the result is not
  `VERIFIED_FLAT`, same-day retries must remain enabled until escalation.
- Track per-day EOD state: `NOT_STARTED`, `SUBMITTING`, `SUBMITTED_NOT_VERIFIED`, `RETRYING`, `VERIFIED_FLAT`, `QUARANTINED`, `MANUAL_REQUIRED`.
- For EOD-triggered flatten, enable bounded cancel/replace/retry regardless of `MANUAL_FLATTEN_WATCHDOG_ENABLED`.
- If still not verified flat after max retries, create an active quarantine/manual lock and emit alert.
- EOD supervisor must not depend on strategy loops or data availability.

Implementation status:

- Landed: `PlatformEODSupervisor` now records per-day state and attempt count, writes `state`
  and `attempt_count` into the machine artifact, and marks the day complete only for
  `VERIFIED_FLAT` or terminal escalation.
- Landed: submit exceptions and `SUBMITTED_NOT_VERIFIED` stay retryable for the same day.
- Landed: `ManualFlattenService` flatten watchdog defaults to enabled for unattended paper/live.
- Landed: terminal `MANUAL_REQUIRED` creates an explicit platform manual-lock JSON artifact
  under `runtime/readiness/manual_locks` and includes `manual_lock_ids` in the EOD artifact.
- Landed: terminal `MANUAL_REQUIRED` writes strategy/symbol locks into the same
  `manual_intervention_lock` store consumed by UOM entry gating.
- Landed: runtime watchdog fails after the EOD cutoff when the EOD artifact is missing,
  no-go, unverified, or has active platform manual locks.
- Landed: `NO_TARGETS` is verified flat only when both DB position truth and broker
  position truth were read successfully. DB/IB truth query failures now produce
  `POSITION_TRUTH_UNAVAILABLE`, `verified_flat=false`, and a retry/manual-required EOD path.
- Landed: runtime watchdog reads PostgreSQL `manual_intervention_lock` when
  `operations.runtime_watchdog.check_db_manual_locks=true`; active locks or lookup failures
  now produce HOLD.
- Landed: runtime watchdog now writes a unified
  `runtime/readiness/production_state/production_state_latest.json` snapshot that includes
  DB manual-intervention locks, EOD state, position safety, system health, runtime
  reconciliation, strategy liveness, and current go/no-go.

Acceptance:

- Simulate a flatten order that remains `SUBMITTED`: supervisor retries or escalates, not one-shot PASS.
- Simulate first submission exception: supervisor retries within same day.
- Simulate broker position remains nonzero after EOD: artifact is `no_go`, active quarantine/manual lock exists, and watchdog reports FAIL.
- Simulate DB or IB position truth query failure at EOD: artifact is `no_go`, not `NO_TARGETS`.
- After `VERIFIED_FLAT`, repeated supervisor loop does not resubmit that day.

Machine artifact:

```json
{
  "artifact_type": "platform_eod_flatten",
  "go_no_go": "go|no_go",
  "state": "VERIFIED_FLAT|RETRYING|QUARANTINED|MANUAL_REQUIRED",
  "attempt_count": 0,
  "verified_flat": false,
  "remaining_position_targets": 0,
  "remaining_open_orders": 0,
  "quarantine_ids": [],
  "order_ids": [],
  "broker_order_ids": []
}
```

### P0.2 Platform Position Safety Invariant

Problem:

- Some strategies implement protection repair locally, but the platform does not yet enforce a universal invariant:

```text
Every nonzero broker position must be protected, flattening, quarantined, or manually locked.
```

Required changes:

- Add a `PositionSafetySupervisor` or extend RuntimeReconciler with a dedicated invariant pass.
- Use broker positions as the outer truth.
- For each nonzero broker position, resolve:
  - owner strategy id, if unambiguous;
  - open protective/close-only/recovery orders;
  - active flatten lifecycle;
  - active quarantine/manual lock.
- If owner is known and strategy manifest declares a protection recovery policy, call it.
- If owner is unknown or ambiguous, quarantine and block new entries for the symbol/root.
- If protection cannot be reconstructed safely, submit close-only flatten through UOM safety lane.

Implementation status:

- Landed: `PositionSafetySupervisor` scans broker positions independently of strategy loops.
- Landed: nonzero broker positions are classified as protected/flattening, quarantined,
  manual-locked, or unsafe.
- Landed: when a nonzero broker position has no active order/quarantine/manual lock, the
  supervisor writes strategy/symbol manual locks for production-blocking strategies that
  declare the symbol.
- Landed: only safety-like working orders count as protected/flattening evidence. Plain
  entry orders no longer make a naked broker position appear safe.
- Landed: safety-order evidence must match the candidate strategy, close the broker-position
  direction, and cover the broker position quantity. Wrong-side, unrelated-strategy, and
  partial-coverage orders do not make a naked broker position safe.
- Landed: DB safety-order evidence must also match broker open-order truth by
  `broker_order_id` or `order_ref`. A stale DB `SUBMITTED` row that IB no longer has does
  not classify a broker position as protected/flattening.
- Landed: `run_system.py` starts the supervisor after execution/UOM/reconciler/EOD are ready.
- Landed: runtime watchdog consumes `runtime/readiness/position_safety/position_safety_latest.json`
  and HOLDs on no-go, lookup failure, or unsafe positions.
- Landed: runtime watchdog also HOLDs when the position-safety artifact is missing or stale,
  including after close/overnight windows.
- Landed: `RecoveryIncidentService` writes durable recovery incident artifacts under
  `runtime/readiness/recovery_incidents`, with one active incident per strategy/symbol lane.
  Position safety now upserts incidents before recovery, marks submitted flatten intents,
  marks manual-required terminal states, and resolves active incidents to `verified_flat`
  when broker truth and quarantine are clean.
- Remaining: add strategy-specific protection repair before falling back to manual lock for
  every unclassified position.

Acceptance:

- Broker-only long position with no owner -> quarantine, no strategy misattribution.
- Broker position with known owner and missing stop -> ensure protection or flatten per manifest.
- Flat broker position with stale protective order -> cancel protective order and terminalize DB state.
- Nonzero broker position after strategy loop stopped -> platform still protects/flattens.

### P0.3 No Production Order Path May Bypass UOM

Problem:

- Main production paths are mostly routed through DecisionPipeline/UOM/ExecutionService.
- `services/exec/options_long_adapter.py` can directly obtain IB exec adapter and place combo orders if `OPTION_ML_EXECUTION_ENABLED=1`.
- Legacy tools/scripts still contain direct IB calls. Some are operational tools, but production startup must prove none are active services.

Required changes:

- Add a startup guard: if execution is enabled, reject any enabled service/strategy using a direct IB execution adapter outside UOM.
- Hard-disable `OPTION_ML_EXECUTION_ENABLED=1` in run_system unless the path is refactored through UOM.
- For option combo execution, create `DecisionEnvelope`/`ExecPlan` support for combo legs or a dedicated UOM-managed combo intent.
- Update `tool/ops/build_uom_caller_surface_report.py` to fail on production-enabled direct IB paths, not only report.

Implementation status:

- Landed: `run_system.py` fails fast when `OPTION_ML_EXECUTION_ENABLED=1`.
- Landed: `services/exec/options_long_adapter.py` raises a hard error instead of placing direct IB combo orders when legacy execution is enabled.
- Remaining: add UOM-managed combo intent support before option combo execution can be re-enabled.

Acceptance:

- `reports/orders/uom_caller_surface_<date>.json` has `production_pending_count=0`.
- Starting run_system with `OPTION_ML_EXECUTION_ENABLED=1` and legacy path enabled fails fast.
- Combo/spread order tests prove DB order row exists before IB submit and can be reconciled by broker order id/perm id/order ref.

## P1: Required Before Larger Paper/Live Expansion

### P1.1 Strategy Production Manifest

Problem:

- A new strategy can be added by implementing strategy code and config, but production safety contracts remain implicit.
- `StrategyRecoveryPolicyRegistry` silently falls back to default close-only flatten for unknown strategies.
- That fallback is safe as a last resort, but not enough as a production admission contract.

Required manifest fields:

```yaml
strategy_id: qqq_intraday_001
production_blocking: true
symbols:
  - QQQ
data_requirements:
  - dataset: ticks
    symbols: [QQQ]
    ttl_sec: 30
  - dataset: bars_5s
    symbols: [QQQ]
    ttl_sec: 20
entry_contract:
  uses_decision_pipeline: true
  uom_only: true
protection_contract:
  required: true
  policy_id: qqq_intraday_protection
  fallback: flatten
recovery_contract:
  policy_id: qqq_intraday_close_only
  unknown_owner_action: quarantine
eod_contract:
  no_overnight: true
  platform_eod_required: true
  strategy_flatten_time_et: "15:58:00"
option_contract:
  required: false
tradability_reporting:
  reports_strategy_tradability_gate: true
```

Required changes:

- Add `StrategyProductionManifest` model and builder from config.
- Add `StockStrategyManifest` on `BaseStockPlugin`, covering symbols, data requirements, sessions,
  protection, recovery, EOD, UOM-only entry, and tradability reporting.
- Add a stocks-kernel config generator that turns a stock plugin manifest into complete strategy
  config, so simple strategies only supply strategy id, symbol/qty/capital, and plugin parameters.
- Startup readiness must reject enabled production-blocking strategies missing required manifest fields.
- Recovery fallback must be explicit `flatten` or `quarantine`; `default_close_only`
  is allowed only as a registered `policy_id`, not as an implicit fallback mode.
- Manifest should feed:
  - DataAvailabilityService;
  - RecoveryReadinessService;
  - RuntimeReconciler owner/recovery policy;
  - Platform EOD;
  - watchdog/go-no-go.

Acceptance:

- Adding a dummy strategy without manifest fails dry startup smoke.
- Adding a strategy with manifest but no recovery policy fails if `recovery_contract.policy_id` is unknown.
- Manifest-derived data requirements produce central data gates.
- Manifest-derived no-overnight policy appears in EOD artifact.

Implementation status:

- Landed: `StrategyProductionManifest` and startup manifest admission gate exist.
- Landed: `BaseStockPlugin.production_manifest()` and `StockStrategyManifest` exist.
- Landed: `NVDAMinuteFlipStockPlugin` declares its own manifest, including NVDA data contract,
  UOM-only entry, close-only recovery, no-overnight EOD, protection fallback, dynamic policy
  registry spec, and tradability reporting.
- Landed: `build_manifest_strategy_instance_config()` generates a full stocks-kernel strategy
  config from the plugin manifest. The NVDA minute-flip test can now be expressed as plugin +
  strategy id + qty/capital/plugin params instead of a large hand-written YAML block.
- Landed: every currently enabled paper strategy in `config.yml` declares `production_manifest`
  and `data_contract`.
- Landed: startup blocks mandatory enabled strategies when manifest, data contract, UOM-only entry,
  recovery, no-overnight EOD, or tradability reporting fields are missing.
- Landed: manifest recovery policy ids are validated against known registered policies.
- Landed: runtime recovery registry is built from production manifests instead of guessing by
  strategy id.
- Landed: enabled strategies now use explicit `qqq_intraday_close_only`,
  `stocks_kernel_close_only`, `qqq_kernel_close_only`, or `option_close_only` policy ids.
- Landed: startup rejects `recovery_contract.fallback: default_close_only`; enabled paper
  strategies must choose explicit `flatten` or `quarantine` fallback behavior.
- Landed: stocks-kernel order lifecycle reconciliation no longer queries `core.core_order.decision_id`.
  Decision-id matching is read from `core.core_order_event` and optional `core_order.note` metadata.
- Landed: integration test `tests/integration/strategies/test_stocks_kernel_db_schema_contract.py`
  verifies the real Postgres schema contract: `core_order` has no `decision_id`, while
  `core_order_event` does.
- Landed: watchdog ledger integrity now flags nonzero UNKNOWN/null-lot rows even when the symbol
  net quantity is zero.
- Landed: watchdog rejects IB restart drill artifacts generated before the current `run_system.py`
  process start time.
- Landed: watchdog rejects IB restart drill artifacts that omit `generated_at`; without a timestamp,
  the artifact cannot be bound to the current process.
- Landed: new IB restart drill artifacts include `generated_at`; watchdog also accepts legacy
  `created_at` as the artifact timestamp and compares it to the current process start.
- Landed: stocks-kernel startup recovery now rechecks broker/DB truth for live-position,
  open-order, and DB residual startup blockers. Once truth is clean, it clears the startup block,
  resets the startup gauge, audits `startup_recovered`, and resumes normal strategy ticks.
- Remaining: add richer strategy-specific protection repair semantics where close-only flatten is
  too conservative.
- Remaining: migrate all new stock strategies to manifest-generated config and add a CLI that prints
  the generated YAML plus validation verdict before admission.

### P1.2 Strategy-Local Fail-Closed Must Feed Central Health

Problem:

- QQQ reports entry tradability into DataAvailabilityService.
- Stocks kernel currently emits local audit rows such as `required_data_missing` / `feature_seed_missing`, but this can remain invisible to central watchdog/go-no-go.

Required changes:

- Provide a common `StrategyContext.report_tradability_block()` helper.
- Require all production-blocking strategies to call it for fail-closed entry blockers.
- Clear the gate only when the strategy proves readiness.
- Watchdog must fail if any production-blocking strategy has active `strategy_tradability` gate.

Acceptance:

- Stocks kernel `history_missing` creates central `strategy_tradability` gate.
- Watchdog reports FAIL while gate is active.
- Safety actions still pass data/tradability gate.

### P1.3 Fresh Production State Heartbeat

Problem:

- Latest `system_resumed` transition is not a heartbeat.
- A watchdog can PASS using stale transition evidence.

Required changes:

- Add a periodic `ProductionStateEmitter` writing every 15-30 seconds:
  - system health state;
  - recovery readiness snapshot and component ages;
  - data availability snapshot;
  - strategy tradability summary;
  - runtime reconciler clean/dirty summary;
  - active quarantine count;
  - EOD supervisor state;
  - broker/order/position truth timestamps.
- `tool/ops/system_health_artifact.py` must mark state unhealthy if older than TTL.
- Runtime watchdog/opening report must consume the same artifact.

Implementation status:

- Landed: `tool/ops/system_health_artifact.py` marks stale health transitions unhealthy.
- Landed: runtime watchdog writes the unified `production_state_latest.json` artifact on
  every run and uses the same source inputs that produce PASS/HOLD.
- Landed: `run_system.py` starts an in-process `ProductionStateEmitter` after execution,
  EOD, and position safety are ready. It refreshes
  `runtime/readiness/production_state/production_state_latest.json` every 30 seconds even if
  the external watchdog is not scheduled.
- Landed: `ProductionStateEmitter` refreshes a SystemHealthController audit heartbeat before
  writing production state, so a stable `running` state does not become stale solely because
  no transition occurred.
- Landed: runtime watchdog checks stale/missing system health on the whole trading day,
  including after close, not only during market session.
- Landed: opening health report now reads the same `production_state_latest.json` artifact
  after the opening grace window. Missing, stale, `no_go`, or failing production state
  blocks opening PASS.

Acceptance:

- Kill bar pipeline heartbeat -> production state turns FAIL within TTL.
- Stop SystemHealthController transition emissions but keep process alive -> watchdog fails stale state.
- Current process liveness alone never produces PASS.

### P1.4 Option Contract and Option Execution Contract

Problem:

- The option kernel has a reasonable contract policy and internal `OPT:...` symbol mapping.
- Legacy option execution paths can still bypass UOM and are not contractually tied to recovery/protection/EOD.

Required changes:

- Define a first-class option order intent:
  - single-leg option;
  - combo/spread;
  - internal symbol;
  - qualified IB conIds;
  - quote source and quote age;
  - width/max loss for defined-risk spreads;
  - close-only/protection/EOD semantics.
- ExecutionService/UOM must persist option order rows before broker submit.
- RuntimeReconciler must map IB option positions to internal symbols and verify ledger ownership.
- Option strategy manifest must declare:
  - DTE policy;
  - strike/delta/liquidity filters;
  - max quote age;
  - spread width/risk;
  - EOD no-overnight policy.

Acceptance:

- Stale option quote blocks entry centrally.
- Ambiguous/failed contract qualification blocks entry and surfaces as data/contract gate.
- Option position from IB maps to `OPT:...` and reconciles to DB.
- Option close-only flatten uses the exact internal symbol or conId, not bare underlying.

### P1.5 Protection Registry Must Be Hard Evidence

Problem:

- UOM writes a protection registry, but write failure is a warning.
- A warning is not enough for a safety invariant.

Required changes:

- For protection orders, if registry write fails after successful broker submit, create immediate quarantine and block entries for that symbol/lot.
- Protection status should reconcile from broker truth, not only DB write success.
- Protection key should include enough identity to avoid false no-op across different lots/sides/qty bands.

Implementation status:

- Landed: after a successful protection submit, UOM registry write failure now creates a
  `manual_intervention_lock` for the strategy/symbol with reason
  `protection_registry_write_failed`; subsequent entries are blocked until resolved.
- Landed: `PositionSafetySupervisor` reconciles active protection registry rows against
  broker open-order truth and current broker positions. Active registry rows are terminalized
  when the broker position is flat or the matching broker open order is gone.

Acceptance:

- Broker protection order submitted but registry write fails -> system enters degraded/quarantine, not PASS.
- Existing protection with different lot/sign/qty does not suppress required new protection.
- Flat position cancels residual protection and terminalizes registry.

### P1.6 Recovery Policy Must Be Explicit and Testable

Problem:

- Runtime recovery can flatten mismatches, but strategy semantics are still mostly default close-only.
- Unknown/ambiguous owner rightly quarantines, but every production strategy should declare its own policy expectations.

Required changes:

- Add recovery policy registry keyed by manifest policy ids.
- Require every production-blocking strategy to declare:
  - owner resolution rules;
  - mismatch behavior;
  - broker-only behavior;
  - missing protection behavior;
  - policy timeout and fallback.
- Emit recovery action artifact with stable `recovery_intent_id`.

Acceptance:

- Known owner mismatch submits exactly one idempotent close-only flatten/protection action.
- Unknown owner mismatch quarantines.
- Policy exception quarantines.
- Repeated reconciler cycles do not stack recovery orders.

## P2: Operational Hardening

### P2.1 Startup and Smoke Tests

Required tests:

- No-external dry load for all enabled strategies.
- Real dependency dry-wire load for all enabled strategies using fake adapters but real StrategyEngine/DecisionPipeline/UOM surfaces.
- Production startup smoke that asserts:
  - decision pipeline ready;
  - UOM wired;
  - runtime reconciler clean;
  - EOD supervisor active;
  - strategy manifests loaded;
  - no enabled strategy missing manifest.

### P2.2 IB Restart Drill Artifact

Required drill:

```text
disconnect
-> broker_api_ready
-> master open orders snapshot
-> positions snapshot
-> market subscriptions restored
-> first tick heartbeat
-> first 5s bar publish
-> data gates clear/block
-> runtime reconcile clean/dirty
-> strategy tradability clean/blocked
-> health running/degraded
```

Required artifact fields:

```json
{
  "artifact_type": "ib_restart_drill",
  "go_no_go": "go|no_go",
  "disconnect_at": "",
  "broker_api_ready_at": "",
  "positions_snapshot_at": "",
  "open_orders_snapshot_at": "",
  "subscriptions_required": [],
  "subscriptions_active": [],
  "first_tick_at": "",
  "first_bar_publish_at": "",
  "data_gate_state": {},
  "runtime_reconcile_state": {},
  "strategy_tradability": {},
  "health_state": "",
  "blockers": [],
  "order_duplicates": 0,
  "orphan_count": 0,
  "ghost_count": 0,
  "position_mismatch_count": 0,
  "active_quarantine_count": 0
}
```

Implementation status:

- Landed: `services.production.ib_restart_drill_artifact.build_ib_restart_drill_artifact`
  builds a machine-checkable artifact from real `runtime/audit/<day>/system-*.jsonl`
  and `recon-*.jsonl`.
- Landed: `tool/ops/build_ib_restart_drill_artifact.py` writes
  `runtime/readiness/drills/paper_ib_restart_latest.json` and a drill-id specific
  JSON file.
- Landed: artifact blocks when disconnect, reconnect-after-disconnect, recovering,
  resumed-after-reconnect, clean runtime reconcile, platform recovery readiness, or
  recovery readiness evidence is missing.
- Landed: current 2026-05-04 paper audit produces `go` with no blockers.
- Landed: runtime watchdog consumes the IB restart drill. If the same-day audit
  contains an IB/market-data disconnect, watchdog requires a same-day `go`
  artifact or returns `HOLD`.
- Remaining: collect the next planned paper IB restart artifact during RTH with
  non-flat/safety-lane scenarios when available.

### P2.3 Operations Dashboards and Alerts

Required alerts:

- stale production state;
- active data/tradability gate in RTH;
- active quarantine;
- broker/order/position truth dirty;
- EOD no-go;
- EOD manual lock active;
- nonzero broker position without protected/flattening/quarantined/manual lock;
- direct IB execution path enabled;
- option quote/contract gate active for enabled option strategy.

## Reference Implementation Order

1. EOD retry/escalation and watchdog hardening.
2. Production state heartbeat and stale watchdog fail.
3. Stocks kernel and all enabled strategies publish central tradability gates.
4. Strategy production manifest and startup admission enforcement.
5. Direct option execution hard-disable or UOM refactor.
6. Platform position safety supervisor.
7. Protection registry hard evidence and quarantine on registry write failure.
8. Option contract/order intent through UOM.
9. IB restart drill artifact. `[landed for real audit replay; keep collecting RTH paper drills]`
10. Full go/no-go dashboard using one production state source.

## P0.6 Strategy Tradability Closure

Problem statement:

- Platform `go` can still mean "order safety substrate is alive" while an enabled
  paper/live strategy is not trade-capable.
- QQQ liveness already computes `tradable=false`, but prior watchdog logic did not
  convert that into `HOLD`.
- QQQ stock-to-option handoff still polls latest source signal state and can spam
  `source_signal_stale` without a durable trigger/ack SLA.
- Option `no_contract_selection` and stocks-kernel `history_missing/feature_not_ready`
  were strategy-local observations instead of central go/no-go blockers.

Required architecture:

- Add a central `strategy_tradability` artifact under `production_state`.
- Every enabled production-blocking strategy must be represented as tradable or
  not tradable with machine-checkable blockers.
- `ProductionState` and watchdog must return `no_go/HOLD` during market session
  when any production-blocking strategy is not tradable.
- Strategy-local blockers must be promoted through `DataAvailabilityService` using
  `dataset=strategy_tradability`.
- QQQ option upstream signal and contract selection blockers must be visible in the
  same central path.

Implementation status:

- Landed: `services.production.strategy_tradability` builds a central snapshot
  from `StrategyDataContract`, `DataAvailabilityService`, and QQQ liveness.
- Landed: runtime watchdog includes `data_availability` and `strategy_tradability`
  in `production_state_latest.json`.
- Landed: watchdog fails during market session after runtime-required time when
  central strategy tradability is `no_go`.
- Landed: QQQ option plugin publishes central `strategy_tradability` gates for
  `source_signal_stale/source_signal_missing/no_contract_selection` and clears
  them after successful option entry submission.
- Landed: QQQ stock-to-option handoff now has trigger-level pending queue semantics.
  Option consumes the oldest non-terminal trigger first, then terminalizes it as
  `submitted`, `contract_rejected`, `expired`, or `risk_blocked`.
- Landed: handoff records are written to `runtime/state/qqq_stock_option_handoff.jsonl`
  with enough `signal_artifact` payload to rebuild pending triggers after a process
  restart. Terminal records suppress repeat consumption after restart.
- Landed: contract-selection rejection payload includes normalized diagnostics such
  as `NO_QUOTES`, `STALE_QUOTE`, `SPREAD_TOO_WIDE`, `DELTA_OUT_OF_RANGE`, `OI_LOW`,
  `QUOTE_AGE`, `CHAIN_EMPTY`, and `RISK_VETO`.

Remaining hardening:

- Promote the JSONL-backed handoff queue to Redis/Postgres if multiple independent
  option consumers are introduced. Current paper runtime has one QQQ option
  consumer, so file-backed replay is sufficient and machine-checkable.
- Add per-strategy signal-to-submit drills for 1DTE option kernels.

Current remaining implementation scope is small and explicit:

- Option combo execution remains hard-disabled until it is represented as UOM/ExecutionService
  order intent.
- Strategy-specific protection repair semantics can be richer than the current conservative
  close-only/manual-lock fallback.
- Continue collecting real RTH paper-session IB restart artifacts, especially with
  non-flat/safety-lane scenarios.

## P0.7 Cleanup / Flat-Account Order Safety Closure

Problem statement:

- A residual cleanup flow must never turn a live broker order into an orphan by
  terminalizing the DB row before broker cancel confirmation.
- A flat broker account with a live close/flatten/protection order is not safe:
  if that order fills later, it can open a reverse position.
- Flatten lifecycle transitions must carry the real `strategy_id`, `symbol`,
  `order_id`, and `order_ref`; `unknown_strategy` evidence is not acceptable
  during flatten verification.

Required architecture:

- Cleanup may request broker cancel, but DB terminalization is allowed only after
  broker truth proves no matching live broker order remains.
- Position safety must check both nonzero broker positions and flat-account live
  safety-like orders. Either condition can make `position_safety` `no_go`.
- Flatten lifecycle state is keyed by the DB order identity and must be
  traceable through broker order id / order ref.

Implementation status:

- Landed: cleanup now lists broker open orders with identity fields and blocks DB
  terminalization when any DB cleanup row still matches a broker live order by
  broker order id or order ref.
- Landed: cleanup writes a `residual_cleanup_blocked` audit event with reason
  `broker_order_still_live_for_db_order` when this invariant fails.
- Landed: `PositionSafetySupervisor` emits `flat_live_orders` and returns
  `no_go` when broker position is flat but a live safety-like order remains.
- Landed: flatten submitted/rejected lifecycle transitions now query and record
  DB `order_id`, `strategy_id`, `symbol`, and `order_ref` instead of emitting
  `unknown_strategy`.

Acceptance tests:

- Cleanup unit test: matching broker order `380` prevents DB order terminalization.
- Position safety unit test: flat NVDA account plus live `NVDA:FLA` order is `no_go`.
- Flatten controller unit test: `mark_submitted_by_idempotency` emits transition
  with real strategy, symbol, and order ref.

## P0.8 Manual Flatten / Protection Price Truth Closure

Problem statement:

- Manual flatten must not use stale/split-wrong ClickHouse closes as execution
  truth when live broker quote is sane.
- Flatten retry must not stack replacement orders while the prior broker order is
  still live or `PendingCancel`.
- ExecutionService cannot validate manual flatten limits only against caller-supplied
  `manual_anchor_px`; it needs an independent quote/anchor conflict gate.
- OCO protection prices must be rejected when they are outside a hard sanity band
  versus entry price or an independently supplied market reference.
- `position_safety` must not emit green when active order/position quarantine exists.

Implementation status:

- Landed: `ManualFlattenService._resolve_ref_px()` now prefers sane broker quote,
  rejects stale anchors that disagree with quote, and uses avg cost before
  ClickHouse fallback when quote is unavailable.
- Landed: manual flatten watchdog waits for broker cancel confirmation before
  submitting a replacement flatten; `PendingCancel` remains live and blocks retry.
- Landed: ExecutionService manual flatten guard rejects quote/anchor conflicts
  instead of using the anchor as source of truth.
- Landed: SafetyKernel validates OCO stop/target prices against entry and optional
  market reference before broker placement, failing closed with a protection lock.
- Landed: `PositionSafetySupervisor` includes active quarantine summary and emits
  `no_go` when any active quarantine exists.

Acceptance tests:

- Manual flatten unit test: stale `1414` anchor with sane `198` quote uses quote
  and rejects the anchor.
- Manual flatten unit test: `PendingCancel` broker order is still considered live
  and prevents replacement retry.
- ExecutionService unit test: manual flatten quote/anchor conflict is rejected.
- SafetyKernel unit test: OCO prices far from market reference are rejected.
- Position safety unit test: active quarantine makes the artifact `no_go`.

## P0.9 Position Cost / Runtime Evidence Closure

Problem statement:

- `core.core_position.avg_px` must not be reconstructed from
  `SUM(position_ledger_current.notional_sum) / SUM(qty)`. Residual adjustment,
  null-price, or historically unmatched lots can leave net qty small while
  notional remains large, corrupting protection and flatten price anchors.
- Stocks-kernel protection payloads need an independent market reference so
  SafetyKernel can reject prices that are sane only relative to corrupted entry
  or ledger avg prices.
- `position_safety` must not be internally green while any record is classified
  as quarantined or has active quarantine.
- Production artifacts must prove the code version that produced them.

Implementation status:

- Landed: SafetyKernel ledger sync now uses `position_ledger_current` for
  quantity only; `avg_px` is maintained by fill-accounting semantics and no
  longer derived from ledger notional.
- Landed: stocks-kernel protection payload includes `market_ref_px`, bid/ask/last
  when available from the execution snapshot.
- Landed: watchdog rejects `position_safety` artifacts that claim `go` while
  records are quarantined, and surfaces active quarantine count directly.
- Landed: production state includes runtime version metadata: git head/dirty,
  process pid/start time, and key module mtimes/sizes.

Acceptance tests:

- SafetyKernel unit test: polluted ledger notional does not corrupt synced
  `core_position.avg_px`.
- Stocks-kernel unit test: protective OCO payload includes `market_ref_px`.
- Watchdog unit test: `go` plus quarantined position-safety record is a failure.
- Watchdog production-state unit test: emitted state includes runtime version.

## P0.10 Strategy Local Truth / Flapping / Option Verdict Closure

Problem statement:

- Platform broker/DB truth can be clean while a strategy-local order lifecycle
  record remains `ACCEPTED`, causing false `ack_not_converged_timeout` and a
  sticky symbol kill switch.
- `ack_not_converged_timeout` must be recoverable after DB/broker truth proves
  the order reached a terminal state and broker isolation is clean.
- Feature callback liveness is not tradability. A strategy must publish its own
  readiness heartbeat that includes data readiness, pending lifecycle, kill
  switches, and runtime blockers.
- IB restart drill must fail on connection flapping, excessive reconnect/resume
  count, slow time-to-resume, or missing post-resume stable window.
- Option linkage must not fall back to latest-signal polling, and contract
  selection rejection must be visible to production go/no-go.

Implementation status:

- Landed: `OrderLifecycleModule` exposes non-terminal records for strategy-level
  truth reconciliation.
- Landed: stocks kernel reconciles non-terminal local lifecycle records against
  `core.core_order` terminal truth before timeout sweep, terminalizes stale local
  records, clears lifecycle freezes, and tries symbol kill-switch release.
- Landed: `ack_not_converged_timeout` / `pending_not_converged_timeout` are
  recoverable kill-switch reasons once broker/account isolation is clean.
- Landed: StrategyEngine feature callbacks no longer write tradable=true
  heartbeats; stocks kernel now emits its own runtime tradability heartbeat with
  kill switch, pending lifecycle, data readiness, and session-aware data contract
  status.
- Landed: IB restart drill artifact includes flapping thresholds, time-to-resume,
  stable-window evidence, and no-go blockers for exceeded thresholds.
- Landed: QQQ option latest-signal fallback is hard-disabled for production even
  if config requests it; the request is recorded as disabled.
- Landed: watchdog/production state consumes `contract_selection_latest.json`
  and turns a fresh rejected/blocked/failed verdict into `HOLD`.

Acceptance tests:

- Order lifecycle unit test: non-terminal records expose only non-terminal order
  refs for DB/broker reconciliation.
- Stocks kernel unit tests: recoverable kill switch release still works; non-
  recoverable kill switch remains sticky.
- Watchdog unit tests: contract selection and strategy tradability paths remain
  machine-checkable.
- IB restart drill unit tests continue to validate required recovery evidence.

## Done Criteria

## P0.11 Startup Lot Adoption / Protection Repair Closure

Problem statement:

- After a restart, broker/core/ledger truth can show a same-strategy live
  position while the strategy in-memory lot ledger is empty.
- The old startup preflight treated every broker live position as a hard block.
  That is correct for foreign or ambiguous positions, but it prevents the
  strategy from reconstructing a verified owned lot and submitting the missing
  protection/flatten repair through the safety lane.
- For leveraged execution symbols, stored entry extras can contain stale
  signal-symbol market refs. Startup adoption must refresh protection payloads
  from the execution symbol quote before any protection rebuild is attempted.

Implementation status:

- Landed: stocks-kernel startup preflight now attempts a strict same-strategy
  broker-position adoption before blocking `broker_live_position_present`.
- Landed: adoption requires broker qty, `core.core_position`, and
  `core.position_ledger_current` to agree for the same `strategy_id/symbol`.
  Missing DB truth, ledger mismatch, open broker orders, or ambiguous ownership
  remains fail-closed.
- Landed: adopted lots are restored into the runtime lot ledger with broker
  identity, ledger lot id, avg price, execution authorities, and
  `protection_lifecycle=repair_pending`.
- Landed: adopted protection payloads are re-anchored to the execution-symbol
  market snapshot, preventing SMH market refs from poisoning SOXL protection
  rebuild prices.
- Landed: DB startup isolation accepts only the just-adopted same-strategy
  positions; foreign positions and unresolved orders still block startup.
- Landed: position safety clears `protection_failed` /
  `position_safety_manual_required` manual locks only after broker truth is
  flat, `core.core_position` is flat, and no non-terminal core order remains.

Acceptance tests:

- Stocks-kernel unit test: same-strategy broker/core/ledger SOXL position is
  adopted into a live lot and its stale market ref is replaced with SOXL quote.
- Stocks-kernel unit test: broker live position without DB truth remains blocked
  as `broker_live_position_present`.
- Existing startup recovery tests still prove pending-cancel and foreign open
  orders are not incorrectly adopted.
- Position-safety unit tests: flat broker/core position clears stale
  `protection_failed` lock; non-flat core position keeps the lock.

## P0.12 Strategy Local Order Truth Convergence

Problem statement:

- Platform truth can converge before a strategy's in-memory order lifecycle
  catches up.
- In the NVDA paper test, DB/broker truth had already terminalized orders and
  flattened the position, but the strategy-local lifecycle could still treat an
  old accepted order as pending and keep a recoverable symbol kill switch active.
- Production go/no-go must not rely on shallow market callbacks, and strategy
  tradability must reflect local blockers such as pending lifecycle records and
  symbol kill switches until they are reconciled from durable truth.

Implementation status:

- Landed: stocks-kernel runtime now reconciles non-terminal local order lifecycle
  records from `core.core_order` before sweeping pending/ack timeouts.
- Landed: if `core.core_order` is not yet terminal, the reconciler falls back to
  terminal `core.core_order_event` rows keyed by `payload.order_ref`.
- Landed: when durable DB/event truth is terminal, the local lifecycle record is
  terminalized, entry freeze is cleared, and recoverable symbol kill switches
  (`ack_not_converged_timeout` / `pending_not_converged_timeout`) are released
  only after broker isolation confirms clean ownership.
- Landed: feature callback heartbeats remain observability-only; strategy
  tradability comes from the strategy heartbeat that includes data readiness,
  pending order lifecycle, and symbol kill switches.

Acceptance tests:

- Stocks-kernel unit test: terminal `core.core_order` state clears a stale local
  lifecycle record and releases the recoverable symbol kill switch.
- Stocks-kernel unit test: terminal `core.core_order_event` fallback does the
  same when the core order row is not terminal yet.

## P0.13 Core Position Residual Convergence

Problem statement:

- `core.core_position` is an operational cache, while broker truth and
  `core.position_ledger_current` are the safer sources for recovery decisions.
- In the SOXL paper run, broker truth and ledger were flat, no non-terminal
  order remained, but `core.core_position.pos_qty` still showed a residual
  position. This correctly blocked strategy startup, but it also proved that
  platform truth needed a conservative cache convergence path.
- In the QQQ option paper run, an exact option contract could be tied to a
  unique strategy order history while generic working-order owner resolution
  still failed. Option ownership must use exact contract evidence, never broad
  underlying-symbol guesses.
- Strategies must not repair this themselves. Cache convergence belongs to a
  platform safety component that can check broker, ledger, and order truth
  together.

Implementation status:

- Landed: `PositionSafetySupervisor` now checks production-blocking
  strategy/symbol pairs for nonzero `core.core_position` residuals.
- Landed: the supervisor clears `core.core_position` only when broker position
  is flat for the symbol, `core.position_ledger_current` net qty is flat, and
  there is no non-terminal `core.core_order`.
- Landed: option `core_position` residuals are also eligible for the same
  conservative cleanup when the owning strategy is production-blocking, even
  though the data contract lists the underlying rather than every generated
  option contract.
- Landed: `PositionReconciler` can resolve a broker-only option position owner
  from a unique recent exact-symbol `core.core_order` for the same option
  contract and expected side. Ambiguous exact-symbol ownership remains
  unresolved and quarantined.
- Landed: runtime recovery close-only flatten is treated like manual emergency
  flatten for RegLite entry-side marketability checks, so a broker-only option
  residual is not trapped forever by `MKT without NBBO snapshot`. It still goes
  through UOM, close-only quantity guards, idempotency, and broker acceptance.
- Landed: after broker/ledger/open-order truth is flat, RuntimeReconciler
  auto-resolves stale position/recovery quarantines for that symbol instead of
  leaving production permanently degraded on old rows.
- Landed: PositionSafetySupervisor clears stale position-mismatch/recovery
  manual locks after broker truth is flat, `core.core_position` is flat, and no
  non-terminal order remains.
- Landed: the repair sets `pos_qty=0`, clears `avg_px`, clears unrealized PnL,
  emits structured logs, and records
  `core_position_residual_cleanup` in `position_safety_latest.json`.
- Landed: if residual lookup fails, `position_safety` becomes `no_go`; DB truth
  cannot be silently skipped.

Acceptance tests:

- Position-safety unit test: stale `core.core_position` is cleared when broker
  and ledger are flat and no working order exists.
- Position-safety unit test: residual is preserved when ledger net qty is
  nonzero.
- Position-safety unit test: residual is preserved when a non-terminal order
  exists.
- Position-safety unit test: stale option `core_position` is cleared for a
  production-blocking option strategy when broker/ledger/order truth are flat.
- Position-reconciler unit test: exact option contract owner is resolved from a
  unique recent strategy order and rejected when ambiguous.
- Risk-service unit test: runtime recovery close-only flatten bypasses RegLite
  `no_nbbo` the same way manual emergency flatten does.
- Runtime reconciler unit test: stale position quarantine is resolved only after
  broker, ledger, DB open orders, and IB open orders are all flat.
- Position-safety unit test: stale position-mismatch manual lock is cleared only
  under the same flat truth constraints.

This work is done only when all are true:

- `make test` passes under the same interpreter used by production runtime.
- Startup smoke loads enabled strategies through the real production wiring dry mode.
- IB restart drill emits `go`.
- EOD flatten failure drill emits `no_go` and active quarantine/manual lock.
- Watchdog fails on stale health, active strategy tradability gate, active quarantine, dirty runtime reconcile, or EOD no-go.
- `production_state_latest.json` includes `strategy_tradability`, and market-session
  `strategy_tradability.no_go` makes the watchdog `HOLD`.
- No production-enabled code path can submit to IB without UOM/ExecutionService.
- Every enabled production-blocking strategy has a manifest.
- No enabled paper/live strategy config contains `shadow` anywhere in the enabled spec.
- Every broker nonzero position is machine-classified as protected, flattening, quarantined, or manually locked.
- Flat broker/ledger plus live close/flatten/protection order is `no_go` until
  the broker order is cancelled, filled-and-reconciled, or re-adopted.
- Cleanup tools cannot terminalize DB order truth unless broker-open-order absence
  is positively verified.

## 2026-05-05 Current-Process Drill And Lot-Ledger Cleanup

Review findings addressed:

- Old same-day IB restart artifacts must not certify a newly started
  `run_system.py` process. The watchdog now requires a paper IB restart drill
  only when an IB disconnect/recovery event is observed after the current
  `run_system.py` process start time. If such an event occurs, the drill
  artifact must still be generated after that process start.
- The SOXL paper run left broker and symbol net truth flat while lot-level
  ledger truth still contained an offsetting null-lot and real-lot split. The
  broker snapshot taken at `2026-05-05T21:37:04Z` proved SOXL/SMH broker
  positions were flat, then the existing residual cleanup convergence path
  appended audited lot-flat adjustment events for
  `stocks_kernel_smh_001/SOXL`.

Implementation status:

- Landed: `_audit_has_ib_disconnect()` accepts a process-start window and ignores
  stale audit rows before the active process. Current-process IB flapping still
  requires a fresh drill artifact.
- Landed: startup readiness reasons such as `startup_ib_ready` and
  `ib_exec_connected` are not treated as disconnect evidence. Only actual
  disconnect/lost/stopped signals can make the current process require an IB
  restart drill.
- Landed: `_ib_restart_drill_status()` records whether the requirement was
  evaluated against the current process window.
- Landed: unit coverage for old disconnects before process start not requiring
  a fresh drill, and current-process disconnects still requiring one.
- Operational cleanup completed: broker-verified SOXL flat snapshot was saved
  under `runtime/readiness/broker_truth/soxl_flat_snapshot.json`, and audited
  residual lot adjustments were appended through the existing cleanup
  convergence helper. Post-cleanup ledger-integrity lookup for
  `stocks_kernel_smh_001/SOXL` returns zero issues, zero account divergence, and
  zero zero-qty notional residuals.
- Landed: stocks-kernel order-lifecycle DB reconciliation no longer passes the
  event-query `decision_id` parameter into the `core.core_order` query. The
  production failure was `the server expects 5 arguments for this query, 6 were
  passed`; terminal `core_order` lookup now has a unit assertion for the real
  five-argument contract.
- Landed: `PositionSafetySupervisor` can now terminalize a stale non-terminal
  DB close/flatten order when broker position is flat, ledger truth is flat, and
  the broker open-order snapshot proves that order is no longer live. This
  unblocks conservative `core.core_position` residual cleanup without allowing
  entry orders to be silently terminalized.

Acceptance:

- A fresh process started after an earlier same-day IB restart is not held by
  the old drill artifact unless the new process itself experiences an IB
  disconnect/recovery.
- If the current process sees an IB disconnect, watchdog blocks until a drill
  artifact with `generated_at >= process_start_time` and `go_no_go=go` exists.
- Broker-flat ledger cleanup must be backed by a fresh broker snapshot and must
  append idempotent adjustment events rather than deleting ledger history.
- Strategy-local order lifecycle may cache pending state, but DB/broker terminal
  truth must be able to clear pending records and release
  `ack_not_converged_timeout` without schema or argument-count drift.
- A broker-flat, ledger-flat residual `core_position` must not remain blocked by
  a stale DB `SUBMITTED` safety order that is absent from broker open-order
  truth; the safety supervisor must terminalize the stale close order first,
  then clear the residual position cache.

## 2026-05-05 Runtime Drill, Option Tradability, And Flat Lock Convergence

Review findings addressed:

- IB restart drill validation is fail-closed, but the runtime must also be able
  to produce the current-process drill artifact automatically. `ProductionStateEmitter`
  now builds a fresh `paper_ib_restart_latest` artifact when the active process
  has post-start disconnect evidence and the existing artifact is missing,
  stale, before process start, or `no_go`.
- Option tradability cannot present stale quote evidence as fully entry-ready.
  Option heartbeat details now separate `idle_ok`, `tradable_for_entry`,
  `data_health_ok`, `data_blockers`, and `readiness_state`.
- Manual flatten can leave a short-lived `manual_flatten` DB lock after the
  account is later verified flat. `PositionSafetySupervisor` now treats
  flat-position `manual_flatten` locks as auto-clearable when broker position,
  core position, and non-terminal order truth are clean.

Acceptance:

- If the current process sees IB disconnect/reconnect/recovery, the next
  production-state heartbeat must create a drill artifact with
  `generated_at >= runtime_version.process_start_time`; watchdog must then
  consume that artifact instead of relying on an external script.
- A same-day old drill artifact may be present, but it cannot certify a process
  that has seen a newer disconnect unless a fresh artifact is generated.
- Option strategy heartbeats may remain operationally `tradable=true` while
  outside the entry window, but must report `readiness_state=idle_not_in_entry_window`,
  `tradable_for_entry=false`, and explicit data health fields when quotes or
  warmup are stale.
- After manual flatten fills and broker/core positions are flat, stale
  `manual_flatten` locks must be cleared by the platform safety loop; watchdog
  must not remain HOLD on a lock whose underlying position/order truth is flat.
