# Order Truth Test Plan (2026-04-23)

## Scope

This test plan covers the missing read-side runtime truth layer for orders.

The first delivery slice is:

1. `OrderTruthService.effective_open_orders(strategy_id)`
2. `RealEngine.open_orders_view()` routing through that service
3. broker-first truth with bounded local submit grace

## Risks Under Test

1. stale DB open order falsely blocks strategy while broker is flat
2. just-submitted local order is not yet visible at broker and would disappear from runtime truth
3. broker open order is visible, but DB metadata still needs to be attached to strategy context
4. broker source unavailable, runtime falls back safely instead of crashing

## Unit Tests

### U1. Broker flat overrides stale DB open row

Given:

- DB has one open order for `qqq_intraday_001`
- broker open orders are empty
- DB row is older than local submit grace

Expect:

- `effective_open_orders("qqq_intraday_001") == []`

### U2. Very recent DB row is preserved during broker visibility gap

Given:

- DB has one open order for `qqq_intraday_001`
- broker open orders are empty
- DB row is within local submit grace

Expect:

- the order remains visible in `effective_open_orders()`

### U3. Broker open order is enriched with DB metadata

Given:

- broker returns an open order with matching `perm_id` or `order_ref`
- DB contains the corresponding order row

Expect:

- returned `OrderUpdate` contains strategy_id, order_ref, order_type, stop/limit fields

### U4. Broker source failure falls back to DB

Given:

- broker open-order source raises

Expect:

- `effective_open_orders()` returns DB projection
- no exception escapes to strategy

### U5. `RealEngine.open_orders_view()` uses truth service when present

Given:

- `RealEngine.order_truth_service` is attached

Expect:

- `open_orders_view()` delegates to it and does not query PG directly

## Regression Tests

1. Existing `tests/unit/services/test_open_orders_view_fields.py`
2. `tests/unit/exec/test_fill_event_handler.py`
3. `tests/unit/exec/test_safety_kernel_position_accounting.py`
4. QQQ strategy tests that depend on `ctx.open_orders()` semantics

## Integration Tests

### I1. Stale DB row does not block QQQ runtime gate

Setup:

- broker flat
- DB contains historical `SUBMITTED` row for QQQ strategy

Expect:

- strategy runtime sees zero effective open orders
- no `legacy_open_orders_block`

### I2. Fill-before-row-visible race

Setup:

- submit order
- fill/status arrives before row is fully visible on the preferred identity path

Expect:

- event is retried or quarantined
- stale historical row is not adopted by `broker_order_id`

## Manual Verification

1. Start paper system with QQQ enabled
2. Inject stale DB open row while broker remains flat
3. Confirm strategy no longer blocks on raw DB row alone
4. Confirm broker-visible live order still blocks as expected
5. Confirm recent local submit is visible during short broker lag window

## Exit Criteria

This test plan is passed when:

1. all new unit tests pass
2. existing targeted execution tests pass
3. QQQ runtime no longer false-blocks on stale DB-only open orders
4. no regression is introduced in legitimate open-order blocking
