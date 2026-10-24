# Manual Flatten Sync TODO

## Scope
- [ ] Route Telegram `清仓` to unified execution path (no direct broker script orders).
- [ ] Keep full traceability for manual intervention:
  - `strategy_id`
  - `trigger_id`
  - `decision_id`
  - `intervention_id`
  - `order_id` / `broker_order_id`
- [ ] Ensure post-flatten reconciliation runs and records outcomes.

## Implementation
- [ ] Add `ManualFlattenService` under `services/telegram/`.
- [ ] Build flatten intents from `core.core_position` (strategy-level close-only).
- [ ] Add broker-only fallback close for symbols missing in DB positions.
- [ ] Emit lifecycle events to `core.core_order_event` for intervention start/end.
- [ ] Wire Telegram bot flatten command to use service callback.
- [ ] Wire `run_system.py` to inject flatten handler after `ExecutionService` init.

## Validation
- [ ] Unit test: strategy-level DB positions are converted to flatten intents.
- [ ] Unit test: broker-only symbols trigger fallback manual flatten intents.
- [ ] Unit test: Telegram command uses callback and no longer requires subprocess.
- [ ] Smoke: verify command output includes intervention id and per-order results.
- [ ] Smoke: verify `core_order_event` has intervention start/end and child order events.

## Acceptance Gates
- [ ] Triggering Telegram flatten does not bypass `ExecutionService`.
- [ ] Orders can be traced to manual intervention in DB/logs.
- [ ] Position-management sync path is executed after flatten request.
- [ ] Existing `账号` command remains unchanged.
