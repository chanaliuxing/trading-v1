# IBKR Order Truth And Reconnect Final Plan (2026-04-17)

## 1. Purpose

This document is the final implementation plan for the current IBKR production failure mode:

1. IB disconnects and cannot stably reconnect
2. clientId drifts across reconnect attempts
3. runtime reconciliation creates or amplifies ghost / orphan behavior
4. operators cannot reliably answer which IB session owns a live order after restart

This document supersedes prior "reconnect-only" thinking. The problem is not just reconnect backoff. The real defect is:

1. reconnect ownership is still partially distributed
2. clientId allocation is still non-deterministic at runtime
3. broker order truth is still session-local instead of account-global
4. runtime reconciler is still capable of destructive action before global broker truth is established

## 2. Facts Confirmed In Code

The following points were re-verified in code and must be treated as current truth:

1. `permId` is already persisted in the main execution path.
   Files:
   - `services/exec/execution_service.py`
   - `services/exec/safety_kernel.py`
2. startup broker readiness gate already exists.
   File:
   - `run_system.py`
3. runtime reconciler still performs destructive ghost cleanup when enabled.
   Files:
   - `services/reconciliation/runtime_reconciler.py`
   - `config.yml`
4. execution adapter still rotates `client_id` after timeout / in-use failures.
   File:
   - `services/exec/adapters/ib_adapter.py`
5. market connection flow still advances client ids after failures.
   File:
   - `services/ibkr/ib_connection_manager.py`
6. fresh probe and owner-cancel probe still create auxiliary IB sessions.
   File:
   - `services/exec/adapters/ib_adapter.py`
7. system still lacks a long-lived `clientId=0` global truth session.
8. sidecar modules still carry independent IB session assumptions or their own client-id namespace:
   - `services/market/options_data.py`
   - `services/market/backfill_service.py`
   - `services/ibkr/vix_ingestor.py`
   - related tools / scripts

## 3. Critical Review Of Earlier Plans

## 3.1 What The Earlier Claude Analysis Got Right

1. The root problem is not a single timeout parameter.
2. `clientId` drift is a first-order cause of ghost orders.
3. reconnect and order ownership are currently decoupled.
4. reconciliation as an automatic cleanup mechanism is unsafe in this failure class.
5. a global broker-truth view is required.

## 3.2 What The Earlier Claude Analysis Needs Correction

1. `permId` is not missing from the system. It already exists in `core.core_order`.
2. startup broker readiness gate is not missing. It already exists in `run_system.py`.
3. the highest-priority schema gap is not `ib_perm_id`; it is:
   - `ib_client_id`
   - `ib_session_role`
   - `ib_connection_epoch`

## 3.3 What The Earlier Codex Plan Got Right

1. The solution must move from reconnect tuning to ownership + identity + truth.
2. Runtime `clientId` drift must be eliminated.
3. A global read-only master truth session is necessary.
4. Reconciler must stop acting like a cleanup engine.
5. Restart recovery must be based on durable order identity, not session-local cache.

## 3.4 What Must Be Corrected In The Earlier Codex Plan

1. Hard-coding `master=0`, `market=10`, `exec=20` without configuration override is too rigid.
2. Removing clientId drift before disabling destructive reconciliation creates a dangerous intermediate state.
3. `connection_epoch` must cover every long-lived IB consumer, not just market-data service and order monitor.
4. "Audit only" is insufficient for unknown broker orders; the system also needs a quarantine mechanism.

## 4. Final Design Decisions

## 4.1 System Invariants

The implementation is only complete when all of the following are true:

1. Only one component owns broker lifecycle:
   - `IBConnectionManager` becomes the only allowed creator / destroyer / recoverer of IB sessions
2. Runtime reconnect never changes the role's clientId
3. Broker truth is available from an account-global, long-lived, read-only session
4. Reconciler cannot mutate broker or DB order state by default
5. Every working order can be attributed to:
   - broker order id
   - perm id
   - owning client id
   - session role
   - connection epoch
6. Any stale IB handle becomes invalid after reconnect epoch changes
7. Unknown live broker orders can block new trading on the affected symbol until resolved

## 4.1.1 Single Trading-Enabled Definition

The system must have exactly one canonical readiness boolean for execution-enabled runtime:

```text
trading_enabled =
    exec_ready
    AND market_ready
    AND master_truth_complete
    AND no_unresolved_quarantine_for_symbol
```

Rules:

1. Business modules must not invent their own independent "ready" logic for trading.
2. Broker connected is not sufficient.
3. Startup adoption incomplete is not sufficient.
4. Presence of unresolved quarantine on a symbol must block new trading on that symbol.

This boolean must be emitted by the broker supervisor / runtime health source and consumed everywhere else.

## 4.2 Canonical Session Model

The final architecture uses role-based sessions managed by one supervisor:

1. `master`
   - read-only
   - account-global truth
   - preferred production default `clientId=0`
   - no market-data subscriptions
   - no order placement
2. `market`
   - shared read session for market-data consumers
3. `exec`
   - sole write-capable execution session
4. `probe`
   - diagnostic only
   - bounded, rate-limited, not part of normal recovery

The actual client ids must come from configuration-backed registry, not hard-coded literals.

## 4.3 ClientId Registry

Introduce `ClientIdRegistry` under `services/ibkr/`.

Responsibilities:

1. map session role -> preferred client id
2. persist resolved mapping to:
   - `runtime/state/ib_client_ids.json`
3. distinguish startup namespace selection from runtime reconnect
4. support environment-specific override

Proposed config:

```yaml
ib:
  registry:
    master_client_id: 0
    market_client_id: 10
    exec_client_id: 20
    strategy_client_id_base: 100
    allow_startup_id_rotation: false
    state_file: runtime/state/ib_client_ids.json
```

Rules:

1. Startup phase may choose a configured fallback range if and only if:
   - current preferred id is occupied
   - the occupant is external to this runtime
   - `allow_startup_id_rotation=true`
2. Runtime reconnect must never rotate to a different clientId for the same role.
3. Existing ad-hoc `ib_client_id` scattered in YAML or tools must be deprecated and then removed.

## 4.4 Master Truth Session

Add a long-lived read-only `master` session owned by `IBConnectionManager`.

Requirements:

1. always connected before execution-enabled runtime enters READY
2. uses:
   - `reqAllOpenOrders`
   - `reqExecutions`
   - optionally `reqCompletedOrders`
3. never used for:
   - order placement
   - cancel
   - market-data subscription
4. provides startup adoption and post-reconnect truth reconciliation

Why this is necessary:

1. session-local `openTrades()` is not enough after clientId drift or restart
2. fresh short-lived probe sessions are diagnostic, not durable system truth

## 4.5 Order Identity Extension

Extend `core.core_order` with:

1. `ib_client_id INT NULL`
2. `ib_session_role TEXT NULL`
3. `ib_connection_epoch INT NULL`

Keep existing:

1. `broker_order_id`
2. `ib_perm_id`
3. `order_ref`

Required write points:

1. `ExecutionService._update_order_submitted`
2. `SafetyKernel` OCO / child order persistence
3. netted execution persistence path
4. adoption path

Rules:

1. all new orders must carry the full identity tuple
2. historical rows may be backfilled with best-effort inference
3. `ib_perm_id` remains the strongest cross-session order identity key, but not the only required one

## 4.6 IBHandle And Connection Epoch

Do not return raw `IB` instances to consumers.

Introduce `IBHandle`:

1. `role`
2. `client_id`
3. `epoch`
4. `ib`

All long-lived consumers must use `IBHandle`, not bare `IB`.

Consumers in scope:

1. `services/market/market_data_service.py`
2. `services/market/ib_market_data.py`
3. `services/portfolio/order_monitor.py`
4. `services/ibkr/vix_ingestor.py`
5. `services/market/options_data.py`
6. `services/market/backfill_service.py`
7. any account-service / sidecar broker consumers

Rules:

1. every successful reconnect increments `connection_epoch`
2. old handles become invalid
3. any use of stale epoch must fail fast and trigger refresh / resubscribe

## 4.7 Reconciler Downgrade

This is the most important sequencing correction.

Before clientId hardening lands, runtime reconciler must be downgraded from actor to detector.

Required changes:

1. remove destructive auto-cleanup behavior from runtime reconciler code path
2. remove broker cancel behavior for unmatched runtime anomalies
3. keep:
   - anomaly detection
   - metrics
   - audit emission
   - quarantine write
4. do not rely on config alone; remove the destructive path from normal runtime operation

Important implementation rule:

1. M1 is a code-deletion milestone, not a config-flip milestone.
2. It is not acceptable to keep destructive runtime reconciler code in the normal path and merely set `auto_cleanup_ghosts=false`.
3. Until the destructive code path is removed from normal runtime flow, no clientId registry / drift-removal change should be promoted to mainline rollout.

Target replacement:

```yaml
reconciliation:
  mode: report_only
  quarantine:
    block_new_intents_on_symbol: true
```

Fields to deprecate and remove after cutover:

1. `reconciliation.cancel_unmatched_orphans`
2. `reconciliation.safe_guard_max_actions`
3. `reconciliation.runtime.auto_cleanup_ghosts`
4. `reconciliation.runtime.ghost_grace_seconds`

## 4.8 Quarantine

Add `core.core_order_quarantine`.

Purpose:

1. hold unknown live broker orders discovered by master truth session or reconciler
2. block new intents on the affected symbol until resolved
3. provide durable operator workflow instead of repeated warning spam

Suggested columns:

1. `quarantine_id UUID`
2. `perm_id BIGINT NULL`
3. `broker_order_id TEXT NULL`
4. `owner_client_id INT NULL`
5. `symbol TEXT NOT NULL`
6. `side TEXT NULL`
7. `qty NUMERIC NULL`
8. `status TEXT NOT NULL`
9. `reason TEXT NOT NULL`
10. `observed_at TIMESTAMPTZ NOT NULL`
11. `resolved_at TIMESTAMPTZ NULL`
12. `resolution TEXT NULL`
13. `payload JSONB NOT NULL DEFAULT '{}'::jsonb`

Operational rules:

1. unresolved quarantine on symbol blocks new entries for that symbol
2. adoption or cancellation resolution must be auditable
3. repeated detection of the same unknown order updates the quarantine row instead of spamming duplicates

## 4.9 Adoption Flow

Startup and recovery adoption must be supervisor-owned.

Flow:

1. master session fetches all open orders
2. build broker indices by:
   - `perm_id`
   - `order_ref`
   - latest `broker_order_id`
3. compare against DB working orders
4. outcomes:
   - DB yes / broker yes:
     - attach truth
     - refresh identity fields
     - verify owner role / epoch
   - DB yes / broker open no:
     - query executions / completed orders before terminalizing
   - DB no / broker yes:
     - write quarantine
     - optionally operator-driven adopt or cancel

Important:

1. runtime reconciler must not own this workflow
2. adoption happens before the rest of the system reaches trading-ready state

## 5. High-Risk Implementation Packages

Only 5 implementation milestones are worth tracking as first-class high-risk packages.

Principle:

1. one milestone = one implementation package
2. package boundary must be codeable
3. do not split further into planning noise unless a package is already in active coding

Supporting work such as baseline capture, dashboards, documentation cleanup, and follow-up removal is not counted as a separate milestone package.

## 5.1 Precondition: Baseline And Instrumentation

Before Package 1 starts:

1. capture current baseline
2. measure:
   - clientId churn
   - reconnect denial / timeout frequency
   - ghost count
   - orphan count
   - fresh-probe rate
   - owner-cancel probe rate

This is a prerequisite, not a milestone package.

## Package 1: Reconciler Disarmament

Goal:

1. remove destructive runtime reconciler behavior from the normal production path
2. replace it with report-only detection plus quarantine emission

Codeable scope:

1. `services/reconciliation/runtime_reconciler.py`
2. `run_system.py`
3. symbol-level block hook in `SafetyKernel` or equivalent entry gate
4. config surface for `reconciliation.mode`

Required outcome:

1. runtime reconciler can no longer auto-terminalize
2. runtime reconciler can no longer auto-cancel
3. unresolved unknown broker order can create quarantine and block new trading on symbol

Reason:

1. this removes the "real position gets killed during migration" failure mode

## Package 2: Master Truth Session And Trading Gate

Goal:

1. add long-lived master truth session
2. make startup adoption a first-class broker-supervisor flow
3. define one canonical `trading_enabled`

Codeable scope:

1. `services/ibkr/ib_connection_manager.py`
2. startup gating in `run_system.py`
3. adoption skeleton
4. runtime health snapshot / gate surface

Required outcome:

1. master session exists and is read-only
2. startup truth snapshot completes before execution-enabled runtime becomes tradable
3. `trading_enabled` is emitted centrally and consumed downstream

## Package 3: Order Identity And Quarantine Schema

Goal:

1. extend durable order identity to include owning session metadata
2. add durable quarantine storage

Codeable scope:

1. DB migration for:
   - `core.core_order`
   - `core.core_order_quarantine`
2. persistence updates in:
   - `ExecutionService`
   - `SafetyKernel`
   - netted execution path
   - adoption path

Required outcome:

1. all new orders persist:
   - `broker_order_id`
   - `ib_perm_id`
   - `ib_client_id`
   - `ib_session_role`
   - `ib_connection_epoch`
2. quarantine is durable and auditable

## Package 4: ClientId Registry And Drift Elimination

Goal:

1. move from runtime clientId drift to deterministic role-based allocation
2. stop normal runtime creation of auxiliary recovery sessions

Codeable scope:

1. `ClientIdRegistry`
2. registry-backed config and state file
3. market connect flow
4. exec connect flow
5. fresh probe / owner-cancel probe behavior
6. sidecar client-id arithmetic cleanup

Required outcome:

1. runtime reconnect never changes role client id
2. startup namespace choice is explicit and config-controlled
3. fresh-probe / owner-cancel paths no longer act as routine recovery mechanism

## Package 5: Supervisor Reconnect Ownership And Epoch Handles

Goal:

1. make broker supervisor the only reconnect owner
2. make all long-lived IB consumers epoch-safe

Codeable scope:

1. `IBHandle`
2. `connection_epoch` lifecycle
3. manager APIs returning handles
4. consumer migration:
   - `market_data_service`
   - `ib_market_data`
   - `order_monitor`
   - `vix_ingestor`
   - `options_data`
   - `backfill_service`
   - other long-lived broker consumers

Required outcome:

1. no business module owns reconnect orchestration
2. no consumer silently uses stale raw `IB`
3. all long-lived consumers recover via new epoch

## 5.2 Post-Package Cleanup

After the 5 packages are complete:

1. remove deprecated config fields
2. remove scattered manual `ib_client_id` settings
3. update operational tools / scripts to use registry or supervisor APIs
4. archive superseded reconnect-only documents

## 6. No-Go Rules

The following are not acceptable intermediate states:

1. clientId drift removed while destructive runtime reconciler is still active
2. master truth session added but business modules still consume stale raw `IB` handles
3. registry introduced while sidecar modules still spawn uncontrolled direct sessions
4. quarantine added without blocking new intents on affected symbol

## 7. Rollback Strategy

Rollback is allowed only to the previous safe point:

1. If M1 fails:
   - restore current reconciler behavior only if production remains unstable without detector
   - otherwise keep report-only and fix forward
2. If M2 or M3 fail:
   - keep report-only reconciler
   - do not proceed to M4
3. If M4 fails:
   - do not re-enable runtime drift
   - keep old stable config and stay on previous registry mapping
4. If M5 fails:
   - keep supervisor as owner where already landed
   - do not restore distributed reconnect ownership

## 8. Exit Criteria

This plan is complete only when:

1. reconnect no longer changes role client ids during runtime
2. startup and recovery rely on master truth, not exec-local openTrades cache
3. runtime reconciler is report-only
4. unknown broker orders land in quarantine and block unsafe new trading
5. all long-lived consumers are epoch-aware
6. 20 forced Gateway/TWS restart drills complete without ghost amplification

## 8.1 Primary System-Level Metrics

Regardless of unit-test breadth, milestone progression is gated primarily by these 3 system-level metrics:

1. `clientId` drift
   - question: does any runtime reconnect still change the role client id?
2. auxiliary session creation
   - question: does the system still auto-spawn fresh-probe / owner-cancel / similar auxiliary sessions in normal runtime recovery?
3. destructive reconciler action
   - question: can runtime reconciler still auto-terminalize or auto-cancel in production runtime flow?

If any of these 3 metrics is not converged, the program must not advance to the next hardening stage.

## 9. Document Map

Use the following companion documents together with this plan:

1. `docs/todo/IBKR_ORDER_TRUTH_AND_RECONNECT_TODO_ACCEPTANCE_2026-04-17.md`
2. `docs/guides/IBKR_ORDER_TRUTH_AND_RECONNECT_TEST_PLAN_2026-04-17.md`
