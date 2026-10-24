## Option ML Execution Review

### option_intraday_perf schema review
- Current fields cover structure metadata, entry/exit, model snapshot, and risk caps.
- Multi-account use can be recorded in `extra_config` without schema change.

### Risk defaults review
- `max_trade_risk_pct=0.005` and `max_daily_dd_pct=0.01` match the stated
  per-trade and daily caps for this strategy.

### Adapter compatibility review
- `process_signals_once` is a single-pass entry point and can be scheduled
  by the existing exec runner or service manager.
