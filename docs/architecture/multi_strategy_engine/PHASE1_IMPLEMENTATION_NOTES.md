# Phase 1 Implementation Notes

## What was implemented
- Execution ingress trace enrichment + shadow validation (non-blocking by default)
- Deterministic idempotency key generation
- `core.core_order.note` trace payload write-through
- `core_order_event` write path (best-effort, non-blocking)
- Netted path trace/idempotency alignment

## Runtime flags
- `execution.trace_validation.enabled` (default: true)
- `execution.trace_validation.blocking` (default: false)
- `execution.trace_validation.paper_force_non_blocking` (default: true)
- `execution.order_event.enabled` (default: true)
- `ALLOW_PAPER_TRACE_BLOCKING=true` can override paper non-blocking safeguard when you intentionally want blocking tests.

## DB migration
Apply:
```bash
psql "$PG_DSN" -f db/pg/migration_20260301_core_order_event.ddl
```

## Trace lookup
```bash
python tools/trace_lookup.py --window-hours 24 --symbol TQQQ --limit 200
```

## Trace gate checks
```bash
python tools/trace_gate_checks.py --window-hours 24
```

## Monday paper preflight
1. Ensure migration is applied:
```bash
psql "$PG_DSN" -f db/pg/migration_20260301_core_order_event.ddl
```
2. Run gate before market open:
```bash
python tools/trace_gate_checks.py --window-hours 24
```
3. During paper run, observe:
- `exec_trace_validation_total{result=...}`
- `exec_trace_missing_total{field=...}`
- `order_event_write_total{event_type=...,result=...}`
4. Verify trace chain samples:
```bash
python tools/trace_lookup.py --window-hours 4 --symbol TQQQ --limit 200
```

## Safety behavior
- If `core.core_order_event` table does not exist, system logs warning and continues execution.
- In paper mode, trace validation is forced non-blocking by default to prevent accidental trade interruption.
