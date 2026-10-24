# QQQ Observability Hardening Checklist (No Trading Logic Change)

## Scope and Guardrails
- Scope: `qqq_intraday_001` observability only.
- Out of scope: entry/exit logic, gate thresholds, order sizing, broker routing behavior.
- Safety rule: any change must be log/diagnostic only and replay-parity validated.

## TODO (Implementation)
1. Add loop lifecycle telemetry:
   - iteration start
   - slow iteration warning
   - cancellation event
   - exception event
2. Add watchdog stall detection:
   - detect long-running iteration (`loop_stall_threshold_sec`)
   - throttle warnings (`loop_stall_warn_interval_sec`)
3. Enforce trace continuity in local audit:
   - fallback chain `trace_id -> decision_id -> trigger_id`
4. Extend heartbeat payload:
   - `loop_iteration_seq`, `loop_last_stage`, `loop_last_duration_ms`
   - readiness fields (`startup_positions_ready`, `broker_positions_ready`, `stream_invalid`, `seed_ready`, `seed_n`)
5. Add local diagnostic checker:
   - `scripts/diag/check_qqq_observability_chain.py`

## Test Plan
1. Unit tests (observability only):
   - `tests/unit/strategies/test_qqq_intraday_observability.py`
     - heartbeat fields present
     - stall detection emits one throttled event
     - local audit fallback IDs are present
2. Safety regression:
   - `tests/unit/strategies/test_qqq_hardening.py`
   - `tests/unit/services/test_runtime_reconciler_polling.py`
3. Runtime smoke (manual):
   - run strategy loop
   - verify `qqq_loop_heartbeat` exists every minute
   - verify `qqq_loop_stall_detected` appears only when forced stall is injected

## Acceptance Criteria
1. **Behavior parity**: replay/baseline trade outcomes unchanged (no decision-path modifications).
2. **Evidence completeness**: if loop cancelled/exception/stall occurs, event is visible in diag and local audit within one cycle.
3. **Trace continuity**: audit events have non-empty trace context (`trace_id` or fallback).
4. **Signal quality**: no high-frequency duplicate stall spam due to warn throttling.
5. **Operational readiness**: checker script returns PASS under healthy run and FAIL when heartbeat is missing.
