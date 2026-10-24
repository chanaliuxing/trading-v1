# Ops Runtime Watchdog Acceptance Checklist (2026-04-24)

## Must Pass

- [x] `build_opening_health_report()` reports `qqq_diag_stale` when QQQ diag freshness exceeds threshold
- [x] `build_opening_health_report()` reports `qqq_runtime_audit_missing` after open grace when `qqq_intraday_001` has no runtime audit rows
- [x] `run_runtime_operations_watchdog.py` reports `opening_artifact_missing:preopen|open|acceptance` when expected files are absent after their checkpoints
- [x] `run_runtime_operations_watchdog.py` reports `telegram_notifier_unavailable` when Telegram credentials are unavailable in the active runtime environment
- [x] watchdog writes a stable JSON state file under `runtime/state/`
- [x] watchdog sends one Telegram `system_alert` when a new issue opens
- [x] watchdog backfills the open alert once notifier availability recovers
- [x] watchdog does not resend the same open issue on every run
- [x] watchdog sends one Telegram recovery notification when the issue clears
- [x] focused unit tests pass

## Runtime Evidence

- [x] During a synthetic stale scenario, watchdog output contains `qqq_strategy_stale`
- [x] During a synthetic missing opening artifact scenario, watchdog output contains `opening_artifact_missing:*`
- [x] `runtime/audit/telegram_push_audit.jsonl` records the corresponding `system_alert`
- [x] A recovery run records a matching INFO recovery event

## Non-Goals Confirmed

- [x] No changes under `strategies/`
- [x] No QQQ signal semantics changed
- [x] No live promotion performed
