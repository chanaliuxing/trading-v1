## Option ML Controls

This document explains how the AI Brain reads/writes `trading.option_ml_controls`
to manage the intraday option ML strategy.

### AI Brain Access Model
- AI Brain must only write to `trading.option_ml_controls`.
- AI Brain reads: `trading.option_intraday_signals`, `trading.option_intraday_perf`,
  `trading.option_ml_models`, and `trading.option_ml_controls`.
- The read helper skill is registered as `option_ml_controls_read`.

### Automation Entry
- Use `tools/option_ml_ai_brain_run.py` for non-trading-hour ML automation (labels → training → backfill → calibration → controls).

### Strategy Identification
- `strategy_name`: must be `intraday_options_long` to match the service.

### Core Enablement
- `enabled`:
  - `0`: disabled, no signals/actions.
  - `1`: enabled for signal generation/execution.
- `mode`: `paper` or `live` for downstream execution routing.

### Model Routing
- `active_model_version`: model version for `clf_dir`/`clf_move`.
- `extra_config.structure_model_version`: model version for structure ranker.

### Thresholds
- `theta_dir`: minimum directional probability to allow action.
- `theta_move`: minimum move probability to allow action.
- `extra_config.min_dir_diff`: minimum gap between `p_up` and `p_down` to choose direction.
- `extra_config.theta_move_credit`: max `p_move` to allow credit/neutral structures.
- `extra_config.min_dir_diff_credit`: minimum bias to choose bull/bear credit.
- `extra_config.max_dir_diff_neutral`: max directional gap to choose neutral structures.

### Risk Caps
- `max_trade_risk_pct`: per-trade risk budget (equity %).
- `max_daily_dd_pct`: daily drawdown cap (equity %).
- `max_trades_per_day`: daily trade count cap.
- `max_consecutive_losses`: stop trading after N consecutive losses.

### Execution Stops
- `stop_loss_pct_on_premium`: premium-based SL (e.g., 0.35).
- `take_profit_pct_on_premium`: premium-based TP (e.g., 0.40).
- `cooldown_min_after_loss`: pause window after a loss (minutes).

### Structure Governance
- `allowed_structures`: JSON array of allowed structure_id.
- `structure_param_caps_json`: JSON map for per-structure parameter caps.
- `max_leg_count`: maximum legs allowed.
- `top_n`: how many signals per minute are allowed to execute.

### Event Filters
- `extra_config.event_blacklist_dates`: list of YYYY-MM-DD dates to disable trading.
- `extra_config.universe_override`: list of symbols to restrict active universe.

### Testing Flags (use only for backfill/debug)
- `extra_config.allow_stale_features`: allow feature lookback beyond 5 minutes.
- `extra_config.allow_stale_signals`: allow reading stale signals in exec.

### Write Pattern
Always insert a new row (do not update in-place). The latest `updated_at`
row is the active control state.
