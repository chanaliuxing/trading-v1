# Ops Runtime Watchdog TODO (2026-04-24)

## Goal

Close the two operational blind spots observed on 2026-04-24:

1. `qqq_intraday_001` can stop updating without any Telegram alert.
2. Opening supervisor artifacts can be missing for the entire day without any explicit escalation.

## Scope

In scope:

1. `qqq_intraday_001` liveness watchdog
2. Opening artifact presence/validity gate
3. Telegram alerts for new failures and recoveries
4. State file for deduped alert transitions
5. Telegram notifier availability gate
6. Focused unit tests

Out of scope:

1. QQQ strategy logic changes
2. IB adapter refactors
3. Market data pipeline redesign
4. Live promotion policy changes

## Implementation TODO

- [x] Extend opening health report with:
  - `qqq_diag` freshness
  - `qqq_runtime_audit` row presence for `qqq_intraday_001`
- [x] Add blockers:
  - `qqq_diag_stale`
  - `qqq_runtime_audit_missing`
- [x] Add runtime operations watchdog:
  - inspect `run_system`
  - inspect `qqq_intraday_001` liveness
  - inspect opening supervisor artifacts
  - inspect Telegram notifier availability in the active runtime environment
  - persist watchdog state
  - emit Telegram `system_alert` on open/recovery transitions
- [x] Add focused unit tests for:
  - stale QQQ strategy detection
  - missing opening artifact detection
  - missing Telegram configuration detection
  - alert transition open -> recovery
- [x] Wire the watchdog into recurring automation cadence
  - cron automation: `runtime-watchdog-open` at 09:45 ET on weekdays

## Test Plan

1. `tests/unit/tool/test_build_opening_health_report.py`
   - verify `qqq_diag_stale`
   - verify `qqq_runtime_audit_missing`
2. `tests/unit/tool/test_run_runtime_operations_watchdog.py`
   - verify missing opening artifact becomes failure
   - verify stale QQQ liveness becomes failure
   - verify Telegram alert emits once on open and once on recovery

## Operator Outcome

After this change, the operator must be told explicitly when:

1. `qqq_intraday_001` stops updating during the market session
2. opening supervisor artifacts are missing past their expected wall-clock checkpoints
3. the above conditions recover
