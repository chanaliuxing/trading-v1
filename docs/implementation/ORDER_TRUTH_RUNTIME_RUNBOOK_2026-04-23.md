# Order Truth Runtime Runbook (2026-04-23)

## Scope

This runbook covers active rows in `core.core_order_quarantine`, especially:

- `unattributed_broker_fill`
- `unattributed_broker_order_status`
- `unknown_broker_order_detected`
- `terminal_orphan_cancel_not_confirmed`

## Operator Rules

1. Do not manually bind a quarantine row to a historical order using `broker_order_id` alone.
2. Prefer `ib_perm_id` and `order_ref`.
3. If neither `ib_perm_id` nor `order_ref` exists, treat the row as unresolved and keep trading blocked for the affected symbol.
4. Resolve only after broker truth and DB truth both show cleared state.

## Triage Steps

1. Inspect active quarantine rows by reason and symbol.
2. For `unattributed_broker_fill`:
   - verify whether `core.core_fill` already contains the `exec_id`
   - verify whether a matching `ib_perm_id` / `order_ref` appeared later
3. For `unattributed_broker_order_status`:
   - verify whether the order is already terminal in broker truth
   - verify whether a DB row exists with matching `ib_perm_id` / `order_ref`
4. If broker and DB are both clear, acknowledge as resolved.
5. If broker still shows a live order and DB is missing it, treat as orphan and keep quarantine active.

## Exit Criteria

A quarantine row may be resolved only when:

- broker no longer shows the order
- DB no longer shows a conflicting working order
- no strategy runtime gate depends on the quarantined identity

## Evidence

The rollout evidence loop must verify:

- unresolved fill/status quarantine code path exists
- runtime reconciliation alert includes active quarantine summary
- this runbook file exists
