# Option Replay Calibration Schema

This document defines the initial live-vs-replay calibration skeleton under `./services/option_replay/core/calibration.py`.

The current scope is intentionally narrow:

- load normalized live fill exports
- build comparable replay fill exports
- match by `side + expiry + strike + time window`
- emit a lightweight calibration report

## Live Fills Input

Required columns in the live fills CSV:

- `fill_ts`
- `side`
- `expiry`
- `strike`
- `qty`
- `fill_price`

Optional columns may exist and are ignored by the current skeleton.

## Replay Comparison Table

`build_replay_fill_comparison_df(result)` emits:

- `fill_ts`
- `side`
- `expiry`
- `strike`
- `qty`
- `fill_price`
- `order_id`
- `fill_reason`

This is the minimal normalized shape used for calibration matching.

## Match Output

`match_live_to_replay_fills(...)` returns one row per live fill:

- `live_idx`
- `replay_idx`
- `matched`
- `time_diff_sec`
- `price_diff`
- `qty_diff`
- `live_fill_ts`
- `replay_fill_ts`
- `side`
- `expiry`
- `strike`

Rules:

- match window is controlled by `CalibrationConfig.match_window_min`
- only one replay fill can match one live fill
- unmatched live fills are preserved with `matched=false`

## Calibration Report

`build_calibration_report(matches)` returns:

- `live_rows`
- `matched_rows`
- `match_rate`
- `avg_time_diff_sec`
- `avg_price_diff`
- `avg_abs_price_diff`
- `avg_qty_diff`

This is a summary skeleton only. It does not yet estimate slippage models or update replay config automatically.

## Intended Next Steps

- support broker-specific adapters such as IBKR exports
- compare order submit/ack/fill times, not just fills
- separate entry fills and exit fills
- calibrate adaptive entry chase and stop slippage using real logs
