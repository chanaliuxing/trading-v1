# Order Truth Acceptance Checklist (2026-04-23)

## Phase A Acceptance

- [x] `RealEngine.open_orders_view()` no longer relies only on raw `core.core_order`
- [x] runtime open-order read path has a dedicated service boundary
- [x] stale DB-only open rows do not appear in effective runtime open orders when broker is flat
- [x] broker-visible open orders still appear in effective runtime open orders
- [x] recent local submit rows remain visible for a bounded grace window before broker visibility
- [x] broker source failure falls back safely without crashing strategies

## Phase B Acceptance

- [x] no live fill/status business attribution path resolves solely by `broker_order_id`
- [x] unresolved broker events are quarantined instead of guessed
- [x] quarantine path emits audit and alert metadata

## Phase C Acceptance

- [x] runtime reconciler publishes effective open-order truth instead of only anomalies
- [x] runtime reconciler publishes conservative flat-only position truth
- [x] strategy gates consume effective truth for open-order and safe-flat position cases
- [x] stale persistence drift remains auditable without poisoning runtime gating

## QQQ-Specific Acceptance

- [ ] `qqq_intraday_001` no longer false-blocks on DB-only stale open orders
- [ ] `qqq_intraday_001` still blocks on real live open orders
- [ ] option downstream no longer receives stale upstream state due only to DB-order ghosting

## Evidence Required

- [x] new unit tests added
- [x] targeted pytest suite green
- [x] machine-readable runtime evidence report added
- [ ] one runtime log sample proving false block removed
- [ ] one runtime log sample proving legitimate block still works
