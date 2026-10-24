# Vegas 1m Sniper V11 Chop Killer

Strategy key: `vegas_hma_setup_trigger_risk_v12`

Runtime wrapper: `strategies.vegas_hma_setup_trigger_risk_v12_strategy:VegasHMASetupTriggerRiskV12Strategy`

Plugin: `strategies.options_plugins.vegas_hma_setup_trigger_risk_v12:VegasHMASetupTriggerRiskV12Plugin`

The module name remains `v12` for config compatibility, but the core signal logic is an implementation of the TradingView `Vegas 1m Sniper V11 [Chop Killer]` script.

## Logic

The strategy consumes 1-minute underlying OHLCV bars and emits option-direction signals:

- `BUY_CALL` for long underlying direction.
- `BUY_PUT` for short underlying direction.
- `EXIT_CALL` / `EXIT_PUT` when the V11 ATR trailing stop is crossed by close.

It does not use the earlier V12 setup-high/setup-low/risk-break state machine. V11 is a direct trigger model:

1. Session filter: `09:35-15:45` America/New_York.
2. HMA and Vegas tunnel indicators.
3. Chop filters: body size and EMA144 slope.
4. Entry quality: first tunnel reclaim with pre-run cap, or pullback-to-fast-HMA followed by a resume candle.
5. Two-bar breakout.
6. Signal state management with one long/short visual state.
7. ATR trailing stop exit based on close.

## Config

Defaults are defined by `VegasHMAConfig`.

| Field | Default |
| --- | --- |
| `fast_len` | `12` |
| `med_len` | `24` |
| `atr_len` | `14` |
| `atr_mult` | `1.80` |
| `chop_body_mult` | `0.30` |
| `slope_mult` | `0.05` |
| `max_dist_from_fhma_atr` | `0.80` |
| `pre_run_lookback` | `18` |
| `max_pre_run_atr` | `1.60` |
| `pullback_lookback` | `8` |
| `pullback_touch_atr` | `0.35` |
| `quality_len` | `120` |
| `trailing_mode` | `percent` |
| `trailing_percent` | `1.5` |
| `use_ibkr_native_trailing_stop` | `true` |
| `signal_only` | `true` |

Older V12 config fields such as `setup_max_bars`, `max_setup_risk_atr`, `trail_start_atr`, and HMA/tunnel fail exits remain on the dataclass for compatibility, but they are not part of the V11 Pine signal path.

## Entry Signals

Long trigger:

- In NY session.
- Fast HMA rising.
- `high > highest(high[1], 2)`.
- `close > tunnel_top + ATR * 0.5`.
- Candle body is greater than `ATR * chop_body_mult`.
- EMA144 5-bar slope is greater than `ATR * slope_mult`.
- `long_quality_ok`.

Short trigger mirrors the long side:

- Fast HMA falling.
- `low < lowest(low[1], 2)`.
- `close < tunnel_bottom - ATR * 0.5`.
- Valid body and negative EMA144 slope.
- `short_quality_ok`.

`long_quality_ok` is true when either:

- The bar is the first reclaim above the tunnel, it is not too extended from fast HMA, and the pre-run from recent lows is within `max_pre_run_atr`.
- A recent pullback touched fast HMA and the current bar is a bullish resume candle with acceptable body quality and distance.

`short_quality_ok` applies the symmetric short-side rules.

## Exit Logic

The V11 stop is close-based ATR trailing:

- On long trigger: `long_stop = close - ATR * atr_mult`.
- Otherwise: `long_stop = max(long_stop, close - ATR * atr_mult)`.
- Long exit: `close < long_stop`.

Short side:

- On short trigger: `short_stop = close + ATR * atr_mult`.
- Otherwise: `short_stop = min(short_stop, close + ATR * atr_mult)`.
- Short exit: `close > short_stop`.

This intentionally does not use the previous V12 fast-HMA fail or tunnel-fail exits.

## IBKR Integration

The strategy does not submit orders directly. Entry signals carry an order intent payload with option direction, underlying reference price, initial underlying stop, strategy id, signal id, and risk metadata.

Exit payloads use the shared `UnderlyingTriggeredOptionExitPlan` contract. The plan keeps these concepts separate:

- `underlying_stop_reference`
- `option_stop_order_spec`
- `conversion_policy`

When native trailing is enabled, the plan includes an IBKR-compatible `TRAIL` order spec using either percent or fixed amount trailing mode. Execution still depends on the adapter consuming that plan; the strategy itself never treats an underlying stop as an option stop price.

## Limitations

This is a Python execution-oriented replica of Pine logic. Indicator details are implemented with local EMA, ATR, WMA, and HMA helpers. Signals are designed to match the Pine conditions and state transitions, while live execution remains governed by the existing option kernel, readiness gates, contract selection, and risk pipeline.
