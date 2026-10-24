# INDEX_SUPER_WIN Isolation Hardening TODO

## 1. Goal

This document focuses on the isolation and runtime-hardening requirements for the new `index_super_win`.

It answers three production questions:

1. Can `index_super_win` run independently when `qqq` is disabled?
2. Can `index_super_win` run multiple different symbols in parallel without state/order conflicts?
3. Can `index_super_win` run alongside `qqq_intraday_001` without position/order contamination?

The highest priority is preventing old failures:

1. Misidentifying another strategy's order as self-owned.
2. Flattening or cancelling `qqq` orders from `index_super_win`.
3. Claiming account-level broker inventory as strategy-owned inventory.
4. Opening repeated or reverse positions because broker/runtime truth drifted.

## 2. Current Status

Already in place:

1. Independent namespace: `iswv2:*`
2. Independent runtime scope: `index_super_win`
3. Independent ownership/quarantine model
4. Entry preview/submit isolation
5. Broker unknown position/open-order quarantine
6. Close-only isolation in `ExecutionService`
7. Runtime reconciler position-sync forced off
8. Observability for signal/block/reconcile/submit path

Not yet fully proven:

1. Same-symbol long-running shadow with `qqq`
2. Multi-symbol restart/recovery under real broker noise
3. Perfect parity with `qqq_intraday` exit/protection lifecycle

## 3. Optimization Targets

## 3.1 Independent Run

Target:

`index_super_win` must run correctly with `qqq` fully disabled.

Required properties:

1. No dependency on `qqq` state files
2. No dependency on `qqqv1` order refs
3. No dependency on `qqq` runtime restore path
4. No hidden assumption that `qqq` process keeps IB or data layer "warm"

## 3.2 Multi-Symbol Run

Target:

One `index_super_win` instance can run `AAPL/MSFT/NVDA/...` simultaneously.

Required properties:

1. Runtime state keyed by `strategy_id + symbol`
2. Ownership keyed by `symbol`
3. Per-symbol preview/submit/reconcile snapshots
4. Per-symbol quarantine isolation
5. Per-symbol observability and replay trail

## 3.3 Parallel Run With QQQ

Target:

`index_super_win` and `qqq_intraday_001` can run together without touching each other's positions or orders.

Required properties:

1. Namespace separation: `iswv2:*` vs `qqqv1:*`
2. Cancel/cleanup path never matches foreign order refs
3. Close-only guard uses strategy-owned position, not account net position
4. Unknown broker position/open-order goes to quarantine, never auto-claimed
5. Same-symbol contention must block new entry, not "take over"

## 4. TODO List

## Phase 1: Isolation Contract Freeze

- [x] Freeze namespace contract:
  - `index_super_win` uses `iswv2:*` only
  - `qqq` continues using `qqqv1:*` only
- [x] Freeze state/runtime path contract:
  - separate runtime scope
  - separate state file paths
  - separate observability paths
- [x] Add explicit audit check that no new `index_super_win` code contains `qqqv1:`
- [x] Add explicit audit check that no `qqq` cleanup path matches `iswv2:`

## Phase 2: Multi-Symbol Safety Hardening

- [x] Add symbol-scoped parity/diagnostic recorder for:
  - preview
  - submit
  - broker reconcile
  - open-order reconcile
  - quarantine transitions
- [x] Add symbol-scoped recovery checks on startup
- [x] Add symbol-scoped stale pending-entry cleanup rules
- [x] Add symbol-scoped restart snapshot consistency check
- [x] Add symbol-scoped protective-order tracking interface for future parity work

## Phase 3: Cross-Strategy Protection

- [x] Add explicit "foreign namespace open order" detection metrics
- [x] Add explicit "foreign symbol ownership already claimed" block reason
- [x] Add explicit "same symbol / different strategy" contention test matrix
- [x] Add same-symbol block report in observability snapshot
- [x] Add shadow report field:
  - `foreign_order_conflicts_detected`
  - `foreign_position_conflicts_detected`

## Phase 4: Recovery Hardening

- [x] Add startup broker snapshot consistency check before entry enable
- [x] Add recovery scenario for:
  - runtime flat + broker non-zero
  - runtime submitted + broker flat
  - broker open orders unknown to runtime
  - DB lag / DB missing rows
- [x] Add controlled recovery state machine report in observability snapshot
- [x] Add restart checkpoint replay harness

## Phase 5: Same-Symbol Shadow Proof

- [ ] Run `qqq_intraday_001` live/shadow baseline
- [ ] Run `index_super_win` shadow on same execution symbol
- [ ] Compare:
  - entry block reason
  - submit timing
  - open-order lifecycle
  - broker reconcile actions
  - cleanup actions
- [ ] Confirm zero foreign cancel / foreign flatten attempts

## 5. Test Plan

## 5.1 Unit Tests

Purpose:

Prove local logic cannot misclassify foreign state.

Cases:

1. `order_ref_belongs_to` rejects foreign namespace
2. `order_ref_belongs_to` rejects foreign strategy_id
3. ownership claim rejects same-symbol foreign strategy owner
4. preview does not claim ownership
5. mark_live_from_broker without submitted intent -> quarantine
6. unknown open orders for same symbol -> quarantine
7. multi-symbol runtime store separation
8. multi-symbol reason counters remain separated
9. close-only guard rejects non-manual flatten when strategy qty is flat
10. manual flatten path can still use broker qty

## 5.2 Integration Tests

Purpose:

Prove service-to-service isolation under realistic flows.

Cases:

1. `index_super_win` standalone startup with `qqq` disabled
2. one `index_super_win` instance with `AAPL/MSFT/NVDA` parallel preview/submit
3. same process with `qqq_intraday_001` + `index_super_win_001`
4. same-symbol foreign open-order detection
5. broker non-zero same-symbol foreign position -> `index_super_win` quarantine
6. `qqq` orphan cleanup does not cancel `iswv2` orders
7. `index_super_win` reconcile does not classify `qqqv1` as owned order
8. execution close-only guard blocks foreign flatten

## 5.3 Failure Injection Tests

Purpose:

Prove fail-safe behavior under broker/runtime drift.

Cases:

1. `ctx.position()` returns `None`
2. `ctx.position()` raises exception
3. broker open-orders fetch fails
4. broker positions refresh fails
5. DB row missing for close-only check
6. broker order id reuse / stale open-order cache
7. restart with stale pending-entry state
8. restart with foreign open orders still alive

Expected behavior:

1. block or quarantine
2. never claim foreign state
3. never submit reverse/flatten against foreign inventory

## 5.4 Shadow Tests

Purpose:

Prove real-session safety before live cut-in.

Sessions required:

1. `index_super_win` alone for 1 full session
2. `index_super_win` multi-symbol shadow for 1 full session
3. `qqq` + `index_super_win` different symbols for 1 full session
4. `qqq` + `index_super_win` same symbol shadow for at least 3 full sessions

Shadow checks:

1. zero foreign cancel actions
2. zero foreign flatten actions
3. zero foreign ownership claims
4. zero reverse-entry incidents
5. zero duplicate entry incidents

## 6. Acceptance Checklist

All items must pass.

## 6.1 Independent Run Acceptance

- [x] `index_super_win` can boot with `qqq` disabled
- [x] `index_super_win` can subscribe and process configured symbols alone
- [x] no `qqq` state path or `qqqv1` dependency appears in runtime logs
- [x] broker reconcile and observability outputs are complete

## 6.2 Multi-Symbol Acceptance

- [x] at least 3 symbols can run concurrently in one strategy instance
- [x] state/preview/reconcile snapshots are separate per symbol
- [x] one symbol quarantine does not block unrelated symbols
- [x] one symbol submit failure does not corrupt another symbol state

## 6.3 QQQ Parallel Acceptance

- [x] `index_super_win` never cancels `qqq` orders
- [x] `index_super_win` never flattens `qqq` positions
- [x] `qqq` never cancels `index_super_win` orders
- [x] same-symbol conflict results in block/quarantine, not order takeover
- [x] execution layer rejects foreign close-only flatten attempts

## 6.4 Recovery Acceptance

- [x] runtime flat + broker non-zero -> quarantine only
- [x] unknown foreign open orders -> quarantine only
- [x] restart does not generate duplicate entry
- [x] restart does not generate reverse entry
- [x] restart does not trigger foreign order cleanup

## 6.5 Observability Acceptance

- [x] can answer "why entered / why blocked" per symbol
- [x] can answer "which gate blocked entry"
- [x] can answer "whether broker/open-order conflict existed"
- [x] can answer "whether conflict was self-owned or foreign"

## 7. Exit Criteria

`index_super_win` may be considered safe for controlled live cut-in only when:

1. all unit/integration/failure-injection tests pass
2. multi-strategy smoke remains green
3. same-symbol shadow with `qqq` passes for at least 3 sessions
4. no foreign cancel / foreign flatten / foreign ownership claim is observed
5. no unresolved quarantine drift remains at session close

## 8. Recommended Implementation Order

1. Freeze isolation contract checks
2. Add missing unit/integration coverage
3. Add failure-injection coverage
4. Add shadow report generation
5. Run standalone shadow
6. Run multi-symbol shadow
7. Run same-symbol parallel shadow with `qqq`
8. Only then consider live cut-in
