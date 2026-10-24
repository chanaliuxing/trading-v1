# Index Super Win Observability Acceptance

## Scope

This acceptance covers the new `index_super_win` observability stack only.

- No `qqq` strategy logic changes
- No execution logic changes
- No reconciliation logic changes
- No market ingest / tick publish logic changes

## Delivered

### 1. Trader View

- `diag_event`
- `diag_snapshot`
- `trader_view_by_symbol`
- `chain_report_by_symbol`
- `shadow_report_by_symbol`

### 2. Local Audit Bridge

System:

- `strategy_started`
- `strategy_ready`
- `startup_consistency_checked`
- `strategy_shutdown_started`

Strategy:

- `entry_intent_requested`
- `entry_intent_submit_failed`
- `entry_intent_rejected`
- `entry_intent_submitted`
- `entry_gate_blocked`
- `preview_evaluated`
- `preview_blocked`
- `runtime_guard_blocked`

Recon:

- `broker_position_quarantined`
- `broker_open_orders_quarantined`
- `startup_recovery_quarantined`

Market:

- `feature_minute_summary`
- `feature_stream_gap`
- `feature_price_missing`

### 3. Query Tools

- `tool/query_audit.py`
- `tool/query_index_super_win_audit_chain.py`

## Guarantees

1. Audit emit is fail-open.
2. Strategy keeps existing `diag_snapshot` explanation layer.
3. Shared `LocalAuditWriter` is reused; no strategy-owned writer lifecycle.
4. `index_super_win` remains namespace-isolated from `qqq`.
5. Audit failures do not block entry / submit / reconcile / shutdown.

## Verified

### Unit / Regression

- `tests/unit/strategies/test_index_super_win_strategy.py`
- `tests/unit/tool/test_query_audit.py`
- `tests/unit/tool/test_query_index_super_win_audit_chain.py`
- `tests/unit/libs/obs/test_audit_writer.py`
- `tests/unit/strategies/test_namespace_isolation_contract.py`
- `tests/unit/exec/test_execution_service_namespace_isolation.py`
- `tests/unit/exec/test_execution_service_close_only_isolation.py`
- `tests/unit/services/strategy_runtime_v2/test_coordinator.py`

### Smoke

- `tool/smoke/test_multi_strategy_isolation_chain.py`

## Latest Results

- Targeted regression: `62 passed`
- Multi-strategy isolation smoke: `10/10 passed`

## Remaining

1. Run live shadow sessions and inspect real audit files.
2. Verify same-symbol `qqq` + `index_super_win` sessions produce the expected blocked evidence chain.
3. Add optional CLI shortcuts / runbook examples for traders if needed.
