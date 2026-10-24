# Order Truth Architecture Remediation TODO (2026-04-23)

## Executive Summary

The execution hardening work that already landed is real and useful, but it mainly upgraded:

1. write-path safety
2. pre-submit gates
3. reconciliation and alerting

It did **not** finish the read-side runtime truth model.

That is why the system could still enter this failure mode:

1. a fill/status attribution race temporarily poisons persistence
2. the reconciler can detect the drift later
3. the strategy gate reads raw persisted open orders before reconciler repair settles
4. the strategy remains fail-closed on stale DB truth even when broker truth is already flat

This document narrows the remaining architecture gap:

> We do not need another large execution rewrite.
> We need a missing runtime order-truth read model and strict identity closure.

## Problem Statement

The current execution stack still allows a single broker fill / status race to poison:

1. `core.core_order`
2. `core.core_fill`
3. `position_ledger_current`
4. `core.core_position`
5. strategy runtime gates that read `ctx.open_orders()`

This is not a strategy-logic bug. It is an order-truth architecture bug.

## Root Cause Summary

### RC1. `broker_order_id` is still treated as a usable identity key

IB `orderId` / `broker_order_id` is session-scoped and reusable. The system still contains fallback paths that query by `broker_order_id` when `perm_id` / `order_ref` is unavailable.

This means a new fill/status event can bind to an old historical order row.

### RC2. Strategy gate reads database "open orders", not broker-truth "effective open orders"

`qqq_intraday_001` blocks entry by calling `ctx.open_orders()`, and `ctx.open_orders()` currently reads `core.core_order WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')`.

That is a persistence snapshot, not an effective broker-truth snapshot.

If persistence is stale, the strategy is blocked even when broker open orders are already zero.

### RC3. Order truth is fragmented across multiple layers

Current flow:

- broker events arrive
- fill/status handler resolves order context
- safety kernel mutates order/fill/ledger
- runtime reconciler later detects drift
- strategy gate separately reads open orders from PG

There is no single authoritative runtime order truth service that all readers must consume.

### RC4. Reconciler is corrective, not authoritative

Runtime reconciler can detect ghosts and mismatches, but strategy reads and fill attribution happen before reconciler finishes.

So the system can fail closed for long periods even after broker truth is already clean.

## Critical Judgement

The current system has been hardened, but it is still fundamentally wrong in one architectural sense:

`core.core_order` is being used as both:

1. append/update persistence
2. strategy gating source
3. fill attribution lookup surface
4. reconciliation working set

Those responsibilities must be split.

## Design Correction

The earlier architecture upgrade was **not wrong**. It was incomplete.

### What the previous upgrade got right

1. `UnifiedOrderManager` as the mandatory write-path shell
2. `PositionLedger` direction for accounting truth
3. `RuntimeReconciler` / `OrderTruthReconciler` for drift detection
4. `SystemHealthController` for global runtime pause/resume

### What it did not close

1. Strategy read-side truth still comes from raw persistence in some paths
2. Reconciler output is diagnostic, not a runtime truth feed
3. Identity rules still allow limited `broker_order_id` fallback
4. QQQ legacy gating still asks the wrong source for open-order truth

### Therefore

The required architecture change is a **read-side completion**, not a restart of the whole execution design.

## Required Architecture Changes

## A. Define a single canonical order identity

Introduce strict identity contract:

- primary key for live broker order lifecycle: `perm_id`
- secondary key for our local intent lineage: `order_ref`
- `broker_order_id` becomes diagnostic only

Rules:

- no business logic may resolve lifecycle by `broker_order_id` alone
- `broker_order_id` may be logged, indexed, and displayed, but never used as an authoritative match key
- if both `perm_id` and `order_ref` are absent, event must enter `unattributed_fill_quarantine`

Implementation tasks:

1. add explicit code comment + invariant checks across execution stack
2. replace remaining `broker_order_id` fallback matches with:
   - `perm_id`
   - else `order_ref`
   - else quarantine
3. add metric: `order_identity_quarantine_total`

## B. Build `OrderTruthService` as the only reader-facing source

Create a single runtime service:

- input:
  - broker open orders snapshot
  - broker fills/status events
  - persisted `core_order`
  - persisted `core_fill`
- output:
  - `effective_open_orders(strategy_id)`
  - `effective_position(strategy_id, symbol)`
  - `order_truth(order_id)`
  - `drift_state(strategy_id)`

Rules:

- strategies must not query raw `core_order` directly for open-order gating
- runtime reconciler must publish into `OrderTruthService`
- `ctx.open_orders()` must be backed by `OrderTruthService.effective_open_orders()`

This removes false blocking caused by stale PG rows.

## C. Split persistence from truth

Current persistence tables remain, but their role changes:

- `core.core_order`: lifecycle journal / persisted state
- `core.core_fill`: fill journal
- `position_ledger_event`: accounting journal
- `OrderTruthService`: runtime truth cache / adjudicated truth

Key rule:

- strategies read runtime truth
- accounting and audit read persisted journals

Not the reverse.

## D. Introduce unattributed event quarantine

When a fill/status arrives without resolvable `perm_id` or `order_ref`, do not guess.

Instead:

- persist event into `core.unattributed_broker_event`
- mark runtime truth as `degraded`
- trigger Telegram alert
- schedule background resolver

This is safer than wrong binding.

## E. Make strategy gating consume "effective open orders"

For `qqq_intraday_001` and later all strategies:

- replace raw PG `state IN ('NEW','SUBMITTED','PARTIAL')` gating
- use `effective_open_orders(strategy_id)` from `OrderTruthService`
- filter by broker-confirmed open orders plus local grace windows explicitly modeled

This is the specific fix that prevents recurrence of the observed false block.

## F. Make runtime reconciler feed truth, not just alerts

Current reconciler mostly emits diagnostics. It must become a truth publisher:

- if broker open orders = 0 and DB has stale open order:
  - publish `effective_state=closed`
  - keep DB row as drifted until repair workflow settles it
- if broker position = 0 and ledger != 0:
  - publish `effective_position=0`
  - mark ledger drift separately

This ensures strategy runtime is not hostage to slower repair.

## G. Separate "trade eligibility" from "ledger consistency"

Today a ledger/order mismatch can block trading indefinitely.

We need explicit policy:

- if mismatch affects current symbol/strategy and could cause duplicate order -> block
- if mismatch is historical and broker-truth is flat -> do not block; raise degraded alert and quarantine repair

This requires a typed blocker model, not boolean checks spread across strategy code.

## Phased TODO

### Phase A: Read-Side Closure

Goal: prevent stale DB rows from directly blocking strategies.

1. [x] Add `services/orders/truth.py`
2. [x] Implement `OrderTruthService.effective_open_orders(strategy_id)`
3. [x] Change `RealEngine.open_orders_view()` to consume `OrderTruthService`
4. [x] Use broker-open snapshot as primary truth when available
5. [x] Keep a short local grace window for just-submitted DB rows not yet visible at broker

### Phase B: Identity Closure

Goal: stop wrong attribution instead of reducing its probability.

1. [x] Remove default business fallback by `broker_order_id` from fill/status attribution
2. [x] Route unresolved events into quarantine
3. [x] Alert on quarantine growth
4. [x] Add operator runbook for replay/adoption

### Phase C: Reconciler-as-Truth-Publisher

Goal: make runtime gating consume reconciled effective truth.

1. [x] Runtime reconciler publishes effective open-order truth
2. [x] Runtime reconciler publishes conservative flat-only position truth
3. [x] Strategies consume effective truth, not raw persisted rows
4. Drift stays visible in audit, but does not automatically poison runtime gating

### Phase D: Runtime Evidence Closure

Goal: prove the new truth plane works in live runtime logs, not only in unit tests.

1. [x] Add machine-readable runtime evidence report for `qqq_intraday_001`
2. [x] Wire runtime evidence into daily trading evidence
3. [ ] Collect one real-session sample with no `legacy_open_orders_block`
4. [ ] Collect one real-session sample showing legitimate non-legacy gate block still appears

### Phase D: Strategy Migration

Goal: remove local truth semantics from strategy code.

1. Migrate QQQ legacy open-order block to `effective_open_orders`
2. Migrate stocks kernel read paths
3. Migrate options kernel read paths
4. Add runtime lint: production strategy code may not read raw `core.core_order` open-state directly

## Immediate Execution TODO

### P0

1. Implement `OrderTruthService` skeleton and runtime API
2. Change `StrategyContext.open_orders()` to read from `OrderTruthService`
3. Remove business matching by `broker_order_id`
4. Add `unattributed_broker_event` quarantine table and handler

### P1

5. Route runtime reconciler outputs into `OrderTruthService`
6. Add `effective_position()` and migrate strategy position gating
7. Add degraded-state alerts for unattributed events / drifted truth

### P2

8. Migrate `qqq_intraday_001` legacy execution gate to `effective_open_orders`
9. Migrate stocks kernel and options kernel to the same truth service
10. Add integration tests for:
   - IB orderId reuse
   - fill-before-row-visible race
   - stale DB open order with broker flat
   - broker reconnect with delayed status replay

## Delivery Notes

### What counts as "done"

This work is only considered done when the runtime path is safer **without relying on manual repair**.

### What does not count

1. More ad hoc grace periods with no ownership boundary
2. More strategy-local checks against raw PG open-order state
3. More repair scripts without read-side truth migration
4. More alerts without runtime behavior change

## Success Criteria

The problem is considered structurally solved only when all are true:

1. no strategy reads raw `core.core_order` as open-order truth
2. no live attribution path can bind by `broker_order_id` alone
3. unattributed events are quarantined, not guessed
4. reconciler publishes effective truth used by runtime gates
5. a stale DB order cannot block a strategy if broker-truth is already flat
