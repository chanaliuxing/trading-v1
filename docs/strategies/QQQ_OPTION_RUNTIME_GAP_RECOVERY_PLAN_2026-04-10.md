# QQQ Option Runtime Gap Recovery Plan (2026-04-10)

## Executive Summary

After multiple rounds of documentation and implementation, the system still does not meet the trader-facing requirement:

- when `qqq_intraday_001` produces a real stock entry signal
- `qqq_intraday_option_v1_001` should receive the same signal
- and decide whether to submit the option order

The current failure is not primarily an architecture-theory problem.
It is a runtime-contract problem:

1. the stock strategy reached `entry_allowed`
2. the shared signal publish path then failed at runtime
3. the option strategy therefore saw `source_signal_missing`
4. the acceptance script still reported `PASS`

That means we have both:

- a real production bug
- a false-positive acceptance framework

## Critical Review

### Finding 1. The upstream signal was not published in live runtime

Evidence from `runtime/audit/2026-04-10/strategy-11.jsonl`:

- `gate_evaluated` with `status=entry_allowed` exists
- immediately after that, `stock_signal_publish_failed` exists
- error payload is:
  - `name 'continuity_ok' is not defined`

First-principles meaning:

- signal logic did fire
- stock strategy did reach the point where option should have been informed
- the publish path crashed before the shared signal artifact was written

So today's missing option trade is not because:

- Redis was down
- option consumer was misconfigured
- no stock signal existed

It is because the producer crashed on the publish path.

## Finding 2. The current acceptance script is materially incomplete

`scripts/check_qqq_option_signal_runtime_acceptance.py` returned `PASS` on the same day that runtime audit shows `stock_signal_publish_failed`.

Current script only proves weak conditions such as:

- strategies are alive
- option logs explicit block reasons
- option signal source mode is `upstream_stock_signal`

It does not prove the critical invariant:

- every stock `entry_allowed` must lead to either:
  - `stock_signal_publish_ok`
  - or a hard `FAIL`

So the current acceptance contract allows a broken producer to pass.

## Finding 3. The documentation marked code completion as requirement completion

Existing docs marked several workstreams as complete because:

- focused tests passed
- config was wired
- observability fields existed

But they did not require same-day runtime proof of:

- stock `entry_allowed`
- producer `publish_ok`
- option consumer `signal_source_status=live`
- downstream `signal_consumed` or explicit trader-valid block after live source consumption

This is the main process failure:

- we closed documents on implementation completion
- not on production-path closure

## Finding 4. QQQ stock runtime still has stability noise unrelated to the publish bug

Historical logs show:

- `qqq_loop_stall_detected`
- multiple strategy reload cycles
- IB timeout noise

These are not the root cause of today's missing option signal.
But they remain material risks because they can:

- delay stock signal production
- reset in-memory state
- create intermittent evidence gaps

So even after fixing the publish exception, the runtime still needs hardening.

## Root Cause Tree

### Primary root cause

- publish-path runtime bug in `qqq_intraday_strategy.py`
- shared signal publication references `continuity_ok`
- the name is not available in the live path where publication now runs

### Secondary root cause

- runtime acceptance criteria are too weak
- they validate consumer observability, not producer success

### Tertiary root cause

- document closure criteria are implementation-centric
- not runtime-outcome-centric

## What This Means

The requirement is still not met.

More specifically:

- `QQQ option` is architecturally closer to the target than before
- but it is not yet operationally reliable
- because the signal authority path is still not proven end-to-end under live runtime

## Recovery Strategy

## P0. Restore the broken live signal path

Goal:

- make `stock entry_allowed -> stock_signal_publish_ok` work again today

Tasks:

1. Fix the `continuity_ok` publish-path runtime bug in `strategies/qqq_intraday_strategy.py`.
2. Add a focused unit test that executes the exact publish branch and fails if publication raises.
3. Add a regression test that proves:
   - `gate_evaluated=entry_allowed`
   - `stock_signal_publish_ok` occurs
   - no `stock_signal_publish_failed` occurs
4. Verify option consumer sees:
   - `signal_source_status=live`
   - non-empty source metadata

Acceptance:

- same-day runtime audit shows at least one `stock_signal_publish_ok` when stock entry is allowed
- no `stock_signal_publish_failed` for the same trigger

## P1. Repair the acceptance framework

Goal:

- stop false green releases

Tasks:

1. Update `scripts/check_qqq_option_signal_runtime_acceptance.py` so it fails when:
   - any `stock_signal_publish_failed` exists on the date
   - stock has `entry_allowed` but zero `stock_signal_publish_ok`
2. Add explicit producer checks:
   - count `stock_signal_publish_ok`
   - count `stock_signal_publish_failed`
   - check mapping from stock `decision_id` or `trigger_id` to publish result
3. Add explicit consumer checks:
   - at least one live source event if producer had publish success
   - fail if all option minutes are `source_signal_missing` after publish success
4. Update docs so a day cannot be called accepted without runtime evidence.

Acceptance:

- the current broken day would return `FAIL`, not `PASS`
- a healthy day returns `PASS`

## P2. Tighten the release contract

Goal:

- prevent “100% done on paper, broken in runtime”

Tasks:

1. Update master TODO / test plan / acceptance docs to separate:
   - code complete
   - runtime proven
2. Introduce mandatory evidence chain:
   - stock `entry_allowed`
   - producer `publish_ok`
   - option `signal_source_status=live`
   - option downstream result
   - submit or trader-valid block reason
3. Mark any document as incomplete if this chain is missing.

Acceptance:

- no future release doc can close without runtime evidence

## P3. Runtime hardening after P0/P1

Goal:

- improve reliability after the immediate blocker is removed

Tasks:

1. Review and reduce `qqq_loop_stall_detected` occurrences.
2. Review repeated strategy reload / restart triggers.
3. Distinguish harmless IB noise from signal-critical IB faults in runtime dashboards.
4. Add one operator-facing daily report:
   - stock signals allowed
   - publish success/failure
   - option live source consumed
   - option submits

Acceptance:

- runtime evidence becomes explainable without manual log archaeology

## Ordered Next Step

Do not start with more architectural refactors.

The next correct sequence is:

1. fix producer runtime bug
2. make acceptance fail on producer failure
3. rerun runtime validation on the same trading day evidence
4. only then continue with stall and resilience hardening

## Definition Of Done

This requirement is complete only when all statements below are true:

1. stock reaches `entry_allowed`
2. shared signal publish succeeds
3. option sees `signal_source_status=live`
4. option downstream result is no longer dominated by `source_signal_missing`
5. runtime acceptance script fails on producer failure and passes on producer success
6. the stable QQQ stock strategy behavior remains unchanged on submit/risk controls

## References

- [QQQ_SIGNAL_DECOUPLING_MASTER_TODO_2026-04-10.md](./docs/strategies/QQQ_SIGNAL_DECOUPLING_MASTER_TODO_2026-04-10.md)
- [QQQ_SIGNAL_DECOUPLING_TEST_PLAN_2026-04-10.md](./docs/strategies/QQQ_SIGNAL_DECOUPLING_TEST_PLAN_2026-04-10.md)
- [QQQ_SIGNAL_DECOUPLING_ACCEPTANCE_CHECKLIST_2026-04-10.md](./docs/strategies/QQQ_SIGNAL_DECOUPLING_ACCEPTANCE_CHECKLIST_2026-04-10.md)
