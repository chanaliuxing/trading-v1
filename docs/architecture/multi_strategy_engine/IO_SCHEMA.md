# I/O Schema Contract

## Scope
Defines canonical payloads for strategy -> execution -> broker/reconciler lifecycle.
Authoritative truth source for trace queries: immutable `core_order_event` table.

## 1) DecisionEnvelope v2 (strategy output)
Required:
- `decision_id`
- `strategy_id`
- `strategy_run_id`
- `trigger_id`
- `trace_id`
- `symbol`
- `target_qty`
- `ts_bar_end_us`

Notes:
- If `trigger_id` missing from legacy strategy, execution ingress may synthesize it.
- `trace_id` should be stable across all downstream events from this decision.
- ID format for generated IDs: UUIDv7 preferred.

## 2) OrderIntent v2 (execution ingress)
Required:
- `intent_id`
- `decision_id`
- `strategy_id`
- `strategy_run_id`
- `trigger_id`
- `trace_id`
- `symbol`
- `side`
- `qty`
- `order_type`
- `submitted_at_us`

Optional:
- `parent_order_id` (required when route mode `netted`)
- `broker_order_id`
- `order_ref`
- `extras`

Idempotency:
- Required `idempotency_key` computed from:
  - `strategy_id + strategy_run_id + decision_id + symbol + normalized_target_qty + ts_bucket`.
- Duplicate `idempotency_key` must not create a second live broker order.

## 3) OrderAuditEvent v1 (immutable lifecycle)
Required:
- `event_id`
- `event_type`
- `event_ts_us`
- `trace_id`
- `causation_id`
- `strategy_id`
- `strategy_run_id`
- `decision_id`
- `intent_id`
- `order_id`
- `symbol`

Typical `event_type`:
- `intent_received`
- `risk_checked`
- `order_submitted`
- `order_ack`
- `order_rejected`
- `fill_received`
- `order_cancelled`
- `reconciler_corrected`
- `invariant_violation`

Critical event field rule:
- For `intent_received`, `order_submitted`, `fill_received`, `reconciler_corrected`,
  fields `strategy_id`, `decision_id`, `trace_id`, `order_id` are mandatory (100% coverage).

## 4) Query Contract for Traceability
### Query by time and order
Input:
- `from_ts`, `to_ts`, `symbol?`, `broker_order_id?`, `order_id?`

Output fields:
- `strategy_id`, `strategy_run_id`, `trigger_id`, `decision_id`, `intent_id`, `order_id`, `broker_order_id`, `event_type`, `event_ts`

SLO:
- Return first page in < 2 seconds for 1 trading day scope.

Query source rule:
- Incident attribution queries must read from `core_order_event` first.
- Log search is auxiliary evidence only.

## Compatibility
- Existing runtime can continue sending old payloads.
- Ingress adapter enriches missing IDs and marks `compat_mode=true`.
