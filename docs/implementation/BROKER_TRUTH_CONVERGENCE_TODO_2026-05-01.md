# Broker Truth Convergence TODO - 2026-05-01

## Problem
Runtime reconciliation currently detects and quarantines broker/DB divergence but does not always converge DB truth to broker truth. A broker-side fill or flat position with missing DB ledger state is a P0 accounting fault, not an observability-only event.

## Phase 0 - Stop Unsafe Trading
- [x] Default startup reconciliation must hard-block order mismatches and position mismatches.
- [x] Runtime startup must not publish `system_resumed` with hard-coded clean truth flags.
- [x] Runtime anomaly events with active order/position quarantine must move system health to `degraded` or `recovering`.
- [x] UOM symbol gate must block new entry/protection/exit intents for symbols with active broker-truth quarantine; cancel and flatten remain allowed.

## Phase 1 - Broker Fill Convergence
- [x] Add `BrokerTruthConvergenceService` as the only writer for reconciliation-created DB facts.
- [x] For broker fills with no attributable DB order, create an adopted reconciliation order using `permId`/`execId` idempotency.
- [x] Persist `core_fill` for the broker execution exactly once.
- [x] Append a position-ledger reconciliation event so `position_ledger_current` matches broker quantity.
- [x] Mark the original quarantine row resolved only after order, fill, and ledger writes are confirmed.

## Phase 2 - Position Ledger Convergence
- [x] Add explicit ledger adjustment events for `broker_qty != ledger_qty`.
- [x] Never overwrite `core_position` or ledger summaries directly.
- [x] Preserve source evidence: account, symbol, broker qty, ledger qty, diff, source, and idempotency key.
- [x] Require manual acknowledgement for strategy attribution when ownership cannot be proven.

## Phase 3 - Open Order Convergence
- [x] Match orphan open orders by `orderRef` before quarantine.
- [x] Sync identity onto an existing non-terminal DB row when matched.
- [x] For unmatched live orders, quarantine and symbol-block by default.
- [x] Open-order adoption remains disabled; no unmatched live order is silently adopted.

## Phase 4 - Operational Cleanup
- [x] Provide an operator command to inspect active divergence by symbol.
- [x] Provide an operator command to apply broker-flat ledger correction for a single symbol after confirmation.
- [x] Add daily evidence that proves no active order quarantine, no active position mismatch, and no ledger/broker mismatch before market open.
