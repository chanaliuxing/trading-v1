# Index Super Win Audit Bridge Plan

## Goal

Upgrade `index_super_win` from diagnostics-only observability to audit-grade local evidence without changing trade logic.

## Principles

- Reuse shared `LocalAuditWriter`; do not create a strategy-owned writer.
- Keep `diag_event` and `diag_snapshot`; audit is additive, not a replacement.
- Fail-open: any audit failure must not block strategy execution.
- Roll out in phases to avoid risk to live strategies.

## Phase 1

Bridge `index_super_win` strategy events into local audit:

- `strategy_started`
- `strategy_ready`
- `startup_consistency_checked`
- `entry_intent_requested`
- `entry_intent_rejected`
- `entry_intent_submit_failed`
- `entry_intent_submitted`
- `strategy_shutdown_started`

Also inject stable trace fields into outbound envelopes:

- `strategy_run_id`
- `trigger_id`
- `trace_id`
- `intent_source=index_super_win`

## Deferred

Phase 2:

- gate-level audit bridge
- preview/blocked reason bridge
- runtime guard audit normalization
- quarantine audit for broker/open-order/startup recovery paths

Implemented:

- `entry_gate_blocked`
- `preview_evaluated`
- `preview_blocked`
- `runtime_guard_blocked`
- `broker_position_quarantined`
- `broker_open_orders_quarantined`
- `startup_recovery_quarantined`

Phase 3:

- query tool by `trace_id/order_id/broker_order_id/symbol/hour`

Implemented:

- `tool/query_audit.py --hour`
- `tool/query_index_super_win_audit_chain.py`

Examples:

```bash
python tool/query_index_super_win_audit_chain.py --date 2026-03-14 --hour 10 --trace-id trace-123 --json
python tool/query_index_super_win_audit_chain.py --date 2026-03-14 --symbol AAPL
python tool/query_audit.py --date 2026-03-14 --hour 10 --broker-order-id 12345
```

Phase 4:

- market minute summary + anomaly audit

Implemented:

- `feature_minute_summary`
- `feature_stream_gap`
- `feature_price_missing`

Scope:

- minute summary from feature bars only
- no raw tick writes
- no changes to market ingest or tick publish path

## Acceptance

- No strategy decision changes
- No gate allow/block result changes
- No submit path behavior changes
- Local audit records can be correlated to execution chain by `trace_id`
