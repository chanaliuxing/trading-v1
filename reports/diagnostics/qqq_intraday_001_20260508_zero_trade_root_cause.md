# qqq_intraday_001 2026-05-08 Zero-Trade Root Cause

## Conclusion

`qqq_intraday_001` did not trade on 2026-05-08 because the live runtime was repeatedly in `system_paused` during the periods where historical replay would have entered.  The replayed market data does produce valid entries, so this was not an all-day lack of QQQ signal.

The hard blockers at the missed replay entry times were external to QQQ:

| Time ET | Live health state | Main hard blockers |
| --- | --- | --- |
| 10:40 | `degraded / production_state_no_go` | `stocks_kernel_smh_001` SOXL `broker_truth_mismatch`, `manual_intervention_lock_active`, `position_safety_no_go`, QQQ `system_paused` |
| 11:47 | `degraded / production_state_no_go` | `stocks_kernel_smh_001` SOXL `broker_live_position_present`, `manual_intervention_lock_active`, `position_safety_no_go` |
| 13:47 | `degraded / production_state_no_go` | `stocks_kernel_smh_001` SOXL `broker_live_position_present`, `manual_intervention_lock_active`, `position_safety_no_go` |

## Evidence

- Live snapshots for `reports/qqq_signal_snapshots/qqq_signal_snapshots_20260508.jsonl` had 75 evaluations and 0 `entry_allowed`.
- Prior sessions were not uniformly blocked:
  - 2026-05-01: 102 evaluations, 8 allowed.
  - 2026-05-05: 137 evaluations, 6 allowed.
  - 2026-05-06: 195 evaluations, 0 allowed.
  - 2026-05-07: 46 evaluations, 0 allowed.
  - 2026-05-08: 75 evaluations, 0 allowed.
- A historical replay from ClickHouse 5-second bars for 2026-05-08 generated 3 trades:
  - 10:40 LONG TQQQ DRIFT
  - 11:47 LONG TQQQ DRIFT
  - 13:47 LONG TQQQ DRIFT
- The replay artifact is under `runtime/diagnostics/qqq_replay_20260508_5s_fallback_logic/`.
- Live heartbeats at those same times had `loop_last_stage=system_paused`.

## Contributing Issues

1. Data continuity was intermittently bad around 10:51, 11:03, 11:13, 11:19, and 11:27 ET.  This explains the `data_integrity_fail_closed / continuity_180s` blocks, but it does not explain the whole zero-trade day.
2. The dominant blocker after 11:36 ET was the SOXL incident owned by `stocks_kernel_smh_001`, which kept production state in `no_go` via manual lock and position safety.
3. The live code version earlier in the day was `2695d7009`; current HEAD `f20c6d57c` contains recovery-latch fixes that filter pure `system_paused` from strategy tradability and allow pause-derived-only production-state recovery.

## Current State

Current readiness artifacts show production state is back to `go`, position safety is `go`, broker position count is 0, active manual lock count is 0, and active quarantine count is 0.

## Recommendation

Keep `position_safety_no_go` and `manual_intervention_lock_active` as global hard blockers unless we explicitly decide to accept cross-strategy isolation risk.  The lower-risk fix path is:

1. Keep the current HEAD recovery-latch fixes deployed.
2. Add/keep regression coverage for pure `system_paused` not becoming a hard production-state latch.
3. Add an operational alert/report that labels missed QQQ replay-entry windows with the owning hard blocker strategy, so the next zero-trade day is immediately attributable.
