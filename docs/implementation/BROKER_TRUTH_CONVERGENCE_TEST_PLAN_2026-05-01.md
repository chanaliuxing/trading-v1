# Broker Truth Convergence Test Plan - 2026-05-01

## Unit Tests
- Startup gate blocks by default when `position_mismatches > 0`.
- Startup gate blocks by default when order state mismatches exist.
- `run_system` resume metadata is derived from reconciler result, not hard-coded clean flags.
- Unattributed broker fill creates adopted order, fill row, and ledger correction once convergence service is enabled.
- Replayed duplicate broker fill does not duplicate `core_fill` or ledger events.
- Broker-flat / ledger-long mismatch appends a ledger adjustment to broker quantity.
- Active position mismatch quarantine blocks new entry/protection intents and allows cancel/flatten intents.
- Runtime reconciliation anomaly publishes `system_degraded`.
- Known orphan open order with matching `orderRef` syncs broker identity before quarantine.
- Terminal DB order with matching `orderRef` is not silently adopted.

## Integration Tests
- Simulate IB open orders with an existing DB row missing `broker_order_id`; reconciler syncs identity and clears quarantine.
- Simulate broker fill before DB order visibility; retry/backfill persists fill after order appears.
- Simulate broker fill that never maps to an order; convergence service creates adopted DB facts and clears mismatch.
- Simulate `TQQQ broker=0, ledger=106`; system remains non-running until a reconciliation adjustment brings ledger to 0.

## Regression Tests
- Existing report-only quarantine tests must be replaced or tightened where they encode unsafe behavior.
- Manual flatten and protective stop cancellation paths must remain cancel-capable even when system health is recovering.
- IB reconnect recovery must only publish `system_resumed` when order truth and position ledger are clean.

## Manual Verification
- Query IB positions and `core.position_ledger_current` for TQQQ before applying correction.
- Apply one audited ledger reconciliation adjustment.
- Run startup reconciliation and confirm `position_mismatches=0`.
- Confirm health event is `system_resumed` only after clean truth.
