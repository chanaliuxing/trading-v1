# IBKR Order Truth And Reconnect TODO / Acceptance (2026-04-17)

## 1. Scope

This is the execution checklist for the IBKR reconnect + ghost-order remediation program.

This document is the authoritative list for:

1. implementation sequencing
2. migration gates
3. acceptance sign-off

## 2. Mandatory Ordering

Only 5 milestone packages are tracked as first-class implementation units:

1. Package 1: reconciler disarmament
2. Package 2: master truth session and trading gate
3. Package 3: order identity and quarantine schema
4. Package 4: clientId registry and drift elimination
5. Package 5: supervisor reconnect ownership and epoch handles

Baseline capture is a prerequisite, not a milestone package.
Cleanup is a follow-up, not a milestone package.

Do not start Package 4 before Package 1 is complete.
Do not merge clientId registry / drift-removal work before Package 1 destructive-path deletion is complete.

## 3. Implementation TODO

## Precondition: Baseline

- [ ] Add baseline counters / queries for:
  - reconnect failures
  - `326` conflicts
  - timeout reconnect attempts
  - fresh-probe count
  - owner-cancel probe count
  - ghost count
  - orphan count
- [ ] Capture 7-day production baseline.
- [ ] Preserve representative logs that show clientId churn.

## Package 1: Reconciler Disarmament

- [ ] Delete destructive ghost cleanup from normal runtime reconciler path.
- [ ] Delete runtime orphan auto-cancel behavior from normal reconciler path.
- [ ] Introduce explicit `reconciliation.mode`.
- [ ] Default mode to `report_only`.
- [ ] Ensure runtime reconciler still emits:
  - audit events
  - metrics
  - notification alerts
- [ ] Add quarantine write path for unresolved unknown broker orders.
- [ ] Wire `SafetyKernel` or equivalent entry guard to block new intents on quarantined symbols.
- [ ] Verify Package 1 is not implemented as config-only disablement.

## Package 2: Master Truth Session And Trading Gate

- [ ] Add `master` role to `IBConnectionManager`.
- [ ] Add `get_master_ib_handle()` or equivalent supervisor API.
- [ ] Ensure master session is read-only by contract.
- [ ] Add startup broker truth snapshot using master session.
- [ ] Build startup adoption scaffolding:
  - DB yes / broker yes
  - DB yes / broker open no
  - DB no / broker yes
- [ ] Ensure system does not enter trading-ready until master truth snapshot completes in execution-enabled modes.
- [ ] Introduce one canonical `trading_enabled` boolean at supervisor/runtime-health layer.
- [ ] Define `trading_enabled` as:
  - `exec_ready`
  - `market_ready`
  - `master_truth_complete`
  - no unresolved quarantine for target symbol
- [ ] Make business modules consume canonical trading-enabled state instead of local readiness guesses.

## Package 3: Order Identity And Quarantine Schema

- [ ] Add migration for `core.core_order`:
  - `ib_client_id`
  - `ib_session_role`
  - `ib_connection_epoch`
- [ ] Add migration for `core.core_order_event` payload / projection support if needed.
- [ ] Add migration for `core.core_order_quarantine`.
- [ ] Update `ExecutionService` submitted-state persistence.
- [ ] Update `SafetyKernel` OCO / child order persistence.
- [ ] Update netted execution persistence path.
- [ ] Update adoption path to fill all identity fields.
- [ ] Add best-effort backfill script for recent working / recent historical orders.

## Package 4: ClientId Registry And Drift Elimination

- [ ] Add `ClientIdRegistry` implementation.
- [ ] Add `ib.registry` config section.
- [ ] Persist resolved mappings to `runtime/state/ib_client_ids.json`.
- [ ] Remove runtime random client-id jumps in:
  - market connect flow
  - exec connect flow
  - fresh probe flow
  - owner-cancel probe flow
- [ ] Distinguish:
  - startup namespace selection
  - runtime reconnect behavior
- [ ] Disable startup id rotation by default in production.
- [ ] Deprecate and then remove scattered strategy / backfill / sidecar `ib_client_id` config.

## Package 5: Supervisor Reconnect Ownership And Epoch Handles

- [ ] Introduce `IBHandle`.
- [ ] Add `connection_epoch` lifecycle to supervisor.
- [ ] Change manager APIs to return handles, not bare `IB`.
- [ ] Convert consumers:
  - `market_data_service`
  - `ib_market_data`
  - `order_monitor`
  - `vix_ingestor`
  - `options_data`
  - `backfill_service`
  - any account-service / sidecar IB consumer
- [ ] Downgrade or remove adapter-local reconnect ownership:
  - `IBInsyncAdapter._reconnector`
  - market-data local reconnect orchestration
- [ ] Ensure stale epoch use fails fast and triggers refresh.

## Post-Package Cleanup

- [ ] Remove deprecated config fields:
  - `reconciliation.cancel_unmatched_orphans`
  - `reconciliation.safe_guard_max_actions`
  - `reconciliation.runtime.auto_cleanup_ghosts`
  - `reconciliation.runtime.ghost_grace_seconds`
- [ ] Remove stale docs that still recommend reconnect-local fixes only.
- [ ] Update runbooks for quarantine / adoption workflow.
- [ ] Update ad-hoc tools and scripts to use registry or supervisor APIs.

## 4. Acceptance Gates

## Gate A: Reconciler Safety

- [ ] Runtime reconciler can no longer auto-terminalize ghost orders in normal production path.
- [ ] Runtime reconciler can no longer auto-cancel unknown broker orders in normal production path.
- [ ] Unknown broker orders produce quarantine records instead of repeated destructive action.
- [ ] Package 1 proof shows destructive code path is removed from normal runtime flow, not merely disabled by config.

## Gate B: Truth Visibility

- [ ] Master session can fetch account-global open orders.
- [ ] Startup adoption runs before execution-enabled runtime becomes READY.
- [ ] Order truth comparison no longer depends on exec-local `openTrades()` alone.
- [ ] `trading_enabled` stays false until master truth is complete.

## Gate C: Identity Completeness

- [ ] All new submitted orders persist:
  - `broker_order_id`
  - `ib_perm_id`
  - `ib_client_id`
  - `ib_session_role`
  - `ib_connection_epoch`
- [ ] Backfilled rows cover all currently working orders.

## Gate D: Runtime Stability

- [ ] Runtime reconnect no longer changes the role's clientId.
- [ ] Forced TWS/Gateway restart drills do not generate new ghost amplification.
- [ ] Auxiliary probe sessions remain bounded and diagnostic-only.

## Gate E: Consumer Safety

- [ ] All long-lived IB consumers are epoch-aware.
- [ ] No consumer silently keeps using a stale `IB` object after reconnect.
- [ ] Market-data subscriptions recover without manual restart after epoch rollover.

## Gate F: Quarantine Safety

- [ ] Unknown broker order creates exactly one active quarantine record.
- [ ] New entry intents on quarantined symbols are blocked.
- [ ] Operator can resolve by adopt or cancel with full audit trail.
- [ ] `trading_enabled` is false for affected symbol while quarantine is unresolved.

## 5. Milestone Metrics

Every milestone review must first answer these 3 system-level questions:

- [ ] Does runtime reconnect still cause `clientId` drift?
- [ ] Does runtime still auto-create fresh-probe / owner-cancel / similar auxiliary sessions?
- [ ] Can runtime reconciler still auto-terminalize or auto-cancel in production flow?

If any answer is "yes", the milestone is not accepted.

## 6. Sign-Off Checklist

- [ ] Engineering sign-off: architecture and migration order accepted.
- [ ] Ops sign-off: runbook updated for quarantine and adoption.
- [ ] Data sign-off: schema migrations reviewed.
- [ ] Trading sign-off: no destructive reconciler behavior remains.
- [ ] Production sign-off: baseline vs post-cutover metrics compared.
