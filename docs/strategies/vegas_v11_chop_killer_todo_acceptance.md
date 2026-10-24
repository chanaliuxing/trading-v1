# Vegas V11 Chop Killer Critical TODO And Acceptance

## Scope

Bring `vegas_hma_setup_trigger_risk_v12` to a faithful implementation of the supplied TradingView Pine V11 script:

`Vegas 1m Sniper V11 [Chop Killer]`

The Python module name remains unchanged for runtime compatibility.

## Critical TODO

- [x] Session must match `time(timeframe.period, "0935-1545:23456", "America/New_York")`.
- [x] ATR must match Pine `ta.atr(14)` semantics: true range plus Wilder RMA seeded by SMA after `atr_len` samples.
- [x] `body_score = atr > 0 ? body / atr : 0` must include early zero values in the dynamic percentile series.
- [x] Warmup must not require 169 bars just because the Vegas tunnel uses EMA169; Pine EMA is available from the first source value.
- [x] Entry signal logic must be only the V11 trigger path, not V12 setup/risk-break state.
- [x] Core signal generation must not block Pine-equivalent signals with non-Pine risk controls such as `daily_max_trades`.
- [x] Long and short stops must update before exit checks, exactly like the Pine script.
- [x] Exits must be close-cross only: `close < long_stop` and `close > short_stop`.
- [x] Plugin live path must keep strategy state coherent when contract selection or submit is rejected.
- [x] Exit intent must carry `exit_plan_payload`, including underlying-triggered option metadata and optional IBKR TRAIL spec.

## Acceptance Tests

- [x] HMA helper warms correctly.
- [x] ATR returns `None` until `atr_len` true range samples, then uses SMA seed and Wilder RMA.
- [x] Session rejects weekends and pre/post session bars.
- [x] Long V11 pullback-resume trigger emits exactly one `BUY_CALL`.
- [x] Short V11 pullback-resume trigger emits exactly one `BUY_PUT`.
- [x] Long and short max-distance filters block extended entries.
- [x] Body-size chop filter blocks weak trigger bars.
- [x] Daily max trades config does not alter core Pine signal generation.
- [x] Long ATR stop moves upward only and exits on close below stop.
- [x] Short ATR stop moves downward only and exits on close above stop.
- [x] Duplicate same timestamp does not emit a second entry.
- [x] Entry rejection rolls back optimistic state.
- [x] No exit plan is emitted without an active runtime position.
- [x] IBKR trailing stop order spec is present when enabled and supports amount mode.
- [x] Options kernel tests prove exit payload is forwarded to execution intent extras.

## Runbook Before Market Open

1. Run targeted tests:
   `venv/bin/python -m pytest tests/unit/strategies/test_vegas_hma_setup_trigger_risk_v12.py tests/unit/strategies/options_kernel/test_base.py tests/unit/strategies/options_kernel/test_plugin_harness.py tests/unit/strategies/test_kernel_contracts.py tests/unit/components/test_indicator_lib.py -q`
2. Run `py_compile` on strategy and plugin files.
3. Confirm `config.yml` has live strategy enabled and `signal_only: false`.
4. Restart `com.trading.run_system.0925`.
5. Confirm Vegas heartbeat exists and reports `tradable_for_entry: true`.
6. Confirm production readiness has no blocker caused by `vegas_hma_setup_trigger_risk_v12_spy_001`.

## Current Verification

- Targeted tests passed: `66 passed`.
- `py_compile` passed for the Vegas strategy, options kernel files, wrapper, and tests.
- Live config parsed successfully with `mode: live`, enabled Vegas strategy, `signal_only: false`, and `enable_live_order_intents: true`.
- Runtime restarted under launchd and loaded `vegas_hma_setup_trigger_risk_v12_spy_001`.
- Post-restart production state: `go`, `failure_count: 0`.
- Current Vegas heartbeat is healthy but outside entry window: `idle_not_in_entry_window`.
