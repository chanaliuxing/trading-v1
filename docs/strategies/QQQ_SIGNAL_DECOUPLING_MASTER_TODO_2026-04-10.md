# QQQ Signal Decoupling Master TODO (2026-04-10)

## Problem Statement

Current behavior couples the QQQ shared entry signal to the stock strategy's own execution path.
As a result, when `qqq_intraday_001` is blocked by stock-only runtime conditions such as:

- active stock open orders
- existing live stock position
- stock-side reconciliation lag

the upstream signal artifact may never be published, and `qqq_intraday_option_v1_001` sees `source_signal_missing`.

This violates the required invariant:

- QQQ option strategy must follow the same QQQ signal logic.
- QQQ stock execution state must not suppress the option strategy's ability to receive that signal.

## First-Principles Invariants

1. Shared signal and execution are different layers.
2. Shared QQQ entry signal must be published once the common QQQ signal logic passes.
3. Stock-only gates may still block stock order submission.
4. Stock-only gates must not block publication of the shared signal artifact.
5. QQQ stock strategy behavior must remain unchanged for:
   - open-order blocking
   - live-position blocking
   - submit readiness
   - actual order submission and risk controls

## Scope

In scope:

- Move stock-only execution gates to after shared signal publication.
- Add explicit artifact metadata that this is a shared entry candidate publication.
- Add regression tests for:
  - open-order blocked stock path still publishes shared signal
  - live-position blocked stock path still publishes shared signal
  - stock submit remains blocked in both cases
- Add implementation-oriented test plan and acceptance checklist.

Out of scope in this change:

- Rewriting the whole signal plane
- Changing option execution rules
- Changing stock-side risk limits or order lifecycle
- Replacing Redis / memory backend behavior

## Detailed TODO

### P0. Safe behavior fix

- [x] Add a small publication-stage marker into the stock signal artifact payload.
- [x] Refactor `QQQIntradayStrategy._maybe_enter()` so stock-only blockers run after shared signal publish.
- [x] Keep all stock submit blockers intact; only their position in the call flow changes.
- [x] Ensure no duplicate submit path is introduced.

### P0. Regression tests

- [x] Add unit test: shared signal publishes even when stock open orders block stock entry.
- [x] Add unit test: shared signal publishes even when live stock position blocks stock entry.
- [x] Assert stock submit is still blocked in both cases.
- [x] Assert artifact payload marks `publish_stage=shared_entry_candidate`.

### P0. Validation

- [x] Run focused stock strategy tests.
- [x] Run upstream option signal consumer tests.
- [x] Verify no failures in existing hardening tests around stock open-order gates.

### P1. Follow-up architecture hardening

- [x] Extract `shared_signal_candidate` evaluation into a dedicated reusable module.
- [x] Split future QQQ architecture into:
  - signal producer
  - stock executor
  - option executor
- [x] Add signal latency audit from stock publish to option consume.

## Progress (Updated 2026-04-10)

- P0 completion: 100% (11/11 items done)
- Overall completion in this document: 100% (14/14 items done)
- Remaining scope: no P0/P1 TODO items open in this document

## Current Risk Register

- Risk: although producer/executor boundaries are now explicit modules, stock submit choreography is still orchestrated from `QQQIntradayStrategy`.
  - Mitigation: future optimization can further extract submit choreography into executor module without changing current behavior contract.
- Risk: latency audit currently measured at option consume time using source trigger timestamp; it is not yet a dedicated end-to-end tracing pipeline.
  - Mitigation: preserve current `signal_source_latency_ms` field and promote it later into strategy-wide tracing when producer module extraction is done.
- Risk: test determinism for upstream signal source can be affected by Redis historical residue.
  - Mitigation: unit tests now force `QQQ_STOCK_SIGNAL_BACKEND=memory` in the upstream source suite.
- Risk: reconciler option-symbol unknown mapping skip behavior now relies on audit visibility for operations.
  - Mitigation: added explicit audit event for `broker_position_unknown` skip path and covered by unit test.

## Expected Outcome

After this change:

- When QQQ shared signal logic passes, option can receive the signal even if stock is blocked by its own open orders or live position.
- QQQ stock strategy still does not place duplicate or unsafe stock orders.
- The system moves one step closer to a pluggable signal/execution architecture without destabilizing the stable stock strategy.
