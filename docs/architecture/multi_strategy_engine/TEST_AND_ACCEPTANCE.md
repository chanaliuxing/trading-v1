# Test, Validation, and Acceptance Plan

## Test Strategy
- Unit: ID generation, schema validation, idempotency, parent-child fanout math.
- Integration: execution service + safety kernel + reconciler with mocked broker.
- E2E: two+ strategies same symbol on paper adapter, with deterministic event replay.
- Regression: existing qqq/index_super_win/v2 flows unchanged with flags OFF.
- Failure injection: duplicate/late/out-of-order broker callbacks, transient DB failures, broker timeout/retry.

## Critical Scenarios
1. Two strategies same symbol, opposite directions, direct mode.
2. Two strategies same symbol, netted mode, partial fills and cancellation.
3. Strategy hot reload midday: same strategy_id new strategy_run_id.
4. Duplicate broker callbacks: ensure no duplicate fills in ledger.
5. Reconciler ghost cleanup does not impact other strategy IDs.

## Required Assertions
- A1: every order can be traced to `(strategy_id, trigger_id, decision_id)`.
- A2: no cross-strategy position mutation under same symbol.
- A3: idempotent retry does not place duplicate broker order.
- A4: fanout allocation sum equals parent fill quantity with documented residual handling.
- A5: all acceptance test logs include `trace_id`.

## Executable Gate Checks
1. Trace coverage (critical fields must be 100%):
```sql
SELECT
  count(*) AS n_total,
  countIf(strategy_id != '' AND decision_id != '' AND trace_id != '' AND order_id != '') AS n_ok
FROM core_order_event
WHERE event_type IN ('intent_received','order_submitted','fill_received','reconciler_corrected')
  AND event_ts >= now() - INTERVAL 1 DAY;
```
Pass rule: `n_ok = n_total`.

2. Parent-child accounting invariant:
```sql
SELECT
  parent_order_id,
  sum(parent_fill_qty) AS p,
  sum(child_fill_qty) AS c,
  abs(sum(parent_fill_qty) - sum(child_fill_qty)) AS diff
FROM netting_fill_audit
WHERE ts >= now() - INTERVAL 1 DAY
GROUP BY parent_order_id
HAVING diff > 0.000001;
```
Pass rule: 0 rows.

3. Cross-strategy mutation check:
```sql
SELECT count(*) AS n_bad
FROM reconciliation_corrections
WHERE correction_ts >= now() - INTERVAL 1 DAY
  AND corrected_strategy_id != source_strategy_id;
```
Pass rule: `n_bad = 0`.

4. Trace query latency:
Command:
`python tools/agent_system/trace_lookup_smoke.py --window 1d --repeat 50`
Pass rule: reported P95 < 2.0s.

5. Idempotency dedupe:
Command:
`pytest -q tests/unit/exec/test_idempotency_dedupe.py`
Pass rule: 100% pass.

6. Failure-injection suite:
Command:
`pytest -q tests/integration/exec/test_failure_injection.py`
Pass rule: 100% pass.

## Acceptance Criteria
- AC1: 100% pass for critical scenarios.
- AC2: 0 P0/P1 defects open.
- AC3: shadow run (5 trading days) has zero unexplained orphan orders.
- AC4: canary run (2 strategy IDs) has no cross-strategy reconciliation errors.
- AC5: rollback drill completes in < 5 minutes with documented steps.
- AC6: rollback drill shows RPO=0 for attribution fields (`strategy_id`, `decision_id`, `trace_id`, `order_id`).
- AC7: OFF-mode parity check passes (feature flags OFF shows no behavior drift in regression suite).

## Suggested Command Matrix
- `make test`
- `pytest tests/unit/exec -v`
- `pytest tests/unit/services/test_runtime_reconciler_* -v`
- `pytest tests/integration -k "strategy or backtest" -v`
- smoke script: submit synthetic intents and verify trace query responses.
- failure injection: `pytest tests/integration/exec/test_failure_injection.py -v`
- off-mode parity: `pytest tests/regression/test_run_system_flag_off_parity.py -v`

## Evidence Package
- test output logs
- trace query screenshots or exported JSON
- reconciler audit events
- canary day-by-day summary
- SQL result snapshots for each executable gate check
- rollback drill timeline and command transcript
