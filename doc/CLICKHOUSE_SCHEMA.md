# ClickHouse Schema Reference
_Generated on 2025-10-05T05:49:38+00:00_

## `bars_10s`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toYYYYMMDD(ts_start)
- ORDER BY (symbol, ts_start)
- TTL ts_start + toIntervalDay(90)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `symbol` | String |  |  |
| `bar_sec` | UInt16 | 10 |  |
| `ts_start` | DateTime |  |  |
| `ts_end` | DateTime |  |  |
| `open` | Float64 |  |  |
| `high` | Float64 |  |  |
| `low` | Float64 |  |  |
| `close` | Float64 |  |  |
| `vwap` | Nullable(Float64) |  |  |
| `volume` | Float64 |  |  |
| `trades` | UInt64 |  |  |
| `bid_last` | Nullable(Float64) |  |  |
| `ask_last` | Nullable(Float64) |  |  |
| `mid_last` | Nullable(Float64) |  |  |
| `ver` | UInt64 |  |  |
| `is_backfill` | UInt8 | 0 |  |
| `created_at` | DateTime | now() |  |

## `bars_1m`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toYYYYMMDD(ts_start)
- ORDER BY (symbol, ts_start)
- TTL ts_start + toIntervalDay(180)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `symbol` | String |  |  |
| `bar_sec` | UInt16 | 60 |  |
| `ts_start` | DateTime |  |  |
| `ts_end` | DateTime |  |  |
| `open` | Float64 |  |  |
| `high` | Float64 |  |  |
| `low` | Float64 |  |  |
| `close` | Float64 |  |  |
| `vwap` | Nullable(Float64) |  |  |
| `volume` | Float64 |  |  |
| `trades` | UInt64 |  |  |
| `bid_last` | Nullable(Float64) |  |  |
| `ask_last` | Nullable(Float64) |  |  |
| `mid_last` | Nullable(Float64) |  |  |
| `ver` | UInt64 |  |  |
| `is_backfill` | UInt8 | 0 |  |
| `created_at` | DateTime | now() |  |

## `bars_5s`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toYYYYMMDD(ts_start)
- ORDER BY (symbol, ts_start)
- TTL ts_start + toIntervalDay(90)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `symbol` | String |  |  |
| `bar_sec` | UInt16 | 5 |  |
| `ts_start` | DateTime |  |  |
| `ts_end` | DateTime |  |  |
| `open` | Float64 |  |  |
| `high` | Float64 |  |  |
| `low` | Float64 |  |  |
| `close` | Float64 |  |  |
| `vwap` | Nullable(Float64) |  |  |
| `volume` | Float64 |  |  |
| `trades` | UInt64 |  |  |
| `bid_last` | Nullable(Float64) |  |  |
| `ask_last` | Nullable(Float64) |  |  |
| `mid_last` | Nullable(Float64) |  |  |
| `ver` | UInt64 |  |  |
| `is_backfill` | UInt8 | 0 |  |
| `created_at` | DateTime | now() |  |

## `dim_symbol`
- ENGINE = MergeTree
- ORDER BY symbol
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `symbol` | LowCardinality(String) |  |  |
| `sector` | LowCardinality(String) |  |  |
| `industry` | LowCardinality(String) |  |  |

## `exec_events`
- ENGINE = MergeTree
- PARTITION BY toYYYYMMDD(ts)
- ORDER BY (strategy_id, symbol, ts, event_id)
- TTL ts + toIntervalDay(365)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `event_id` | String |  |  |
| `ts` | DateTime |  |  |
| `strategy_id` | String |  |  |
| `symbol` | String |  |  |
| `order_id` | String |  |  |
| `event_type` | Enum8('SUBMITTED' = 1, 'PARTIAL' = 2, 'FILLED' = 3, 'CANCELLED' = 4, 'REJECTED' = 5) |  |  |
| `qty` | Float64 |  |  |
| `price` | Nullable(Float64) |  |  |
| `broker_order_id` | Nullable(String) |  |  |
| `details` | String |  |  |
| `created_at` | DateTime | now() |  |

## `features_10s`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toYYYYMMDD(ts)
- ORDER BY (symbol, ts)
- TTL ts + toIntervalDay(90)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `symbol` | String |  |  |
| `ts` | DateTime |  |  |
| `close` | Float64 |  |  |
| `vwap` | Float64 |  |  |
| `atr_14` | Nullable(Float64) |  |  |
| `atr_5` | Nullable(Float64) |  |  |
| `ema_10` | Nullable(Float64) |  |  |
| `ema_20` | Nullable(Float64) |  |  |
| `ema_50` | Nullable(Float64) |  |  |
| `volume` | Float64 |  |  |
| `vol_ema_20` | Nullable(Float64) |  |  |
| `vol_spike` | Nullable(Float64) |  |  |
| `z_price_20` | Nullable(Float64) |  |  |
| `z_vol_20` | Nullable(Float64) |  |  |
| `spread_bps` | Nullable(Float64) |  |  |
| `slope_20` | Nullable(Float64) |  |  |
| `r2_20` | Nullable(Float64) |  |  |
| `ver` | UInt64 |  |  |
| `created_at` | DateTime | now() |  |
| `ret_1` | Nullable(Float64) |  | 1-period return |
| `ret_5` | Nullable(Float64) |  | 5-period return |
| `log_ret` | Nullable(Float64) |  | Log return |
| `roc_5` | Nullable(Float64) |  | Rate of change 5-period |
| `roc_10` | Nullable(Float64) |  | Rate of change 10-period |
| `sma_5` | Nullable(Float64) |  | Simple moving average 5-period |
| `sma_20` | Nullable(Float64) |  | Simple moving average 20-period |
| `sma_60` | Nullable(Float64) |  | Simple moving average 60-period |
| `std_20` | Nullable(Float64) |  | Standard deviation 20-period |
| `std_60` | Nullable(Float64) |  | Standard deviation 60-period |
| `z_price_60` | Nullable(Float64) |  | Price z-score 60-period |
| `bb_mid_20` | Nullable(Float64) |  | Bollinger Band middle (20-period SMA) |
| `bb_up_20` | Nullable(Float64) |  | Bollinger Band upper (mid + 2*std) |
| `bb_dn_20` | Nullable(Float64) |  | Bollinger Band lower (mid - 2*std) |
| `bb_bw_20` | Nullable(Float64) |  | Bollinger Band width ratio |
| `bb_pct_20` | Nullable(Float64) |  | Bollinger %B (position within bands) |
| `stoch_k_14` | Nullable(Float64) |  | Stochastic %K (14-period) |
| `stoch_d_3` | Nullable(Float64) |  | Stochastic %D (3-period SMA of %K) |
| `rsi_14` | Nullable(Float64) |  | Relative Strength Index (14-period) |
| `rv_20` | Nullable(Float64) |  | Realized volatility (20-period) |
| `parkinson_20` | Nullable(Float64) |  | Parkinson volatility estimator (20-period) |
| `garman_klass_20` | Nullable(Float64) |  | Garman-Klass volatility estimator (20-period) |
| `donchian_hi_20` | Nullable(Float64) |  | Donchian channel high (20-period) |
| `donchian_lo_20` | Nullable(Float64) |  | Donchian channel low (20-period) |
| `di_plus_14` | Nullable(Float64) |  | Directional Indicator +DI (14-period) |
| `di_minus_14` | Nullable(Float64) |  | Directional Indicator -DI (14-period) |
| `dx_14` | Nullable(Float64) |  | Directional Movement Index (14-period) |
| `adx_14` | Nullable(Float64) |  | Average Directional Index (14-period) |
| `vol_avg_20` | Nullable(Float64) |  | Volume average (20-period) |
| `rvol_roll_20` | Nullable(Float64) |  | Relative volume: current / 20-period avg |
| `rvol_tod_20d` | Nullable(Float64) |  | Relative volume: current / same time-of-day avg (20 days) |
| `cmf_20` | Nullable(Float64) |  | Chaikin Money Flow (20-period) |
| `mfi_14` | Nullable(Float64) |  | Money Flow Index (14-period) |
| `obv_60` | Nullable(Float64) |  | On-Balance Volume (60-period window) |
| `vwap_day` | Nullable(Float64) |  | Intraday VWAP (session cumulative) |
| `vwap_dev_bps` | Nullable(Float64) |  | Deviation from VWAP in basis points |
| `vwap_slope_20` | Nullable(Float64) |  | VWAP trend strength (20-period correlation) |
| `micro_mid` | Nullable(Float64) |  | Microprice mid (from L1) |
| `close_to_mid_bps` | Nullable(Float64) |  | Close price deviation from mid in bps |
| `body` | Nullable(Float64) |  | Candlestick body (close - open) |
| `upper_wick` | Nullable(Float64) |  | Upper wick (high - max(open,close)) |
| `lower_wick` | Nullable(Float64) |  | Lower wick (min(open,close) - low) |
| `range` | Nullable(Float64) |  | High-low range |
| `range_pct` | Nullable(Float64) |  | Range as percentage of close |
| `bar_sec` | UInt16 |  | Bar size in seconds |
| `minute_of_day` | UInt16 |  | Minute of day (0-1440) |
| `open` | Nullable(Float64) |  | Open price |
| `high` | Nullable(Float64) |  | High price |
| `low` | Nullable(Float64) |  | Low price |
| `vwap_bar` | Nullable(Float64) |  | Bar VWAP |
| `trades` | UInt32 |  | Number of trades |
| `vix` | Nullable(Float64) |  |  |
| `vix_sma_20` | Nullable(Float64) |  |  |
| `vix_z_20` | Nullable(Float64) |  |  |
| `vix_chg_1m` | Nullable(Float64) |  |  |
| `vix_chg_5m` | Nullable(Float64) |  |  |

## `features_1m_labeled`
Intraday option ML labels derived from 1m bars.
- ENGINE = MergeTree
- PARTITION BY toDate(ts)
- ORDER BY (symbol, ts, horizon_min)
- TTL ts + INTERVAL 365 DAY
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `ts` | DateTime64(3, 'America/New_York') |  |  |
| `symbol` | String |  |  |
| `horizon_min` | UInt16 |  | e.g. 30 |
| `ret_h` | Float64 |  | Forward return over horizon |
| `rng_h` | Float64 |  | Forward range over horizon |
| `y_dir` | Int8 |  | -1/0/1 direction label |
| `y_move` | UInt8 |  | 0/1 move label |
| `ret_threshold` | Float64 | 0.0 | Direction threshold used |
| `range_threshold` | Float64 | 0.0 | Move threshold used |
| `created_at` | DateTime('America/New_York') | now() |  |

## `option_ml_models`
Model registry for intraday option ML.
- ENGINE = ReplacingMergeTree(created_at)
- ORDER BY (model_version)

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `model_version` | String |  | e.g. options_long_v1_2026-01-05 |
| `created_at` | DateTime('America/New_York') | now() |  |
| `train_start` | DateTime('America/New_York') |  |  |
| `train_end` | DateTime('America/New_York') |  |  |
| `universe` | Array(String) |  |  |
| `horizon_min` | UInt16 |  |  |
| `feature_set_name` | String | features_1m |  |
| `val_samples` | UInt64 |  |  |
| `val_accuracy_dir` | Float64 | 0.0 |  |
| `val_precision_up` | Float64 | 0.0 |  |
| `val_precision_down` | Float64 | 0.0 |  |
| `val_recall_up` | Float64 | 0.0 |  |
| `val_recall_down` | Float64 | 0.0 |  |
| `val_auc_move` | Float64 | 0.0 |  |
| `val_coverage` | Float64 | 0.0 |  |
| `pnl_per_trade` | Float64 | 0.0 | Offline proxy |
| `max_dd_pct` | Float64 | 0.0 | Offline proxy |
| `theta_dir` | Float64 | 0.6 | Suggested production threshold |
| `theta_move` | Float64 | 0.65 | Suggested production threshold |
| `meta` | String | {} | JSON metadata |
| `vix_rsi_14` | Nullable(Float64) |  |  |

## `features_5s`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toYYYYMMDD(ts)
- ORDER BY (symbol, ts)
- TTL ts + toIntervalDay(90)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `symbol` | String |  |  |
| `ts` | DateTime |  |  |
| `close` | Float64 |  |  |
| `vwap` | Float64 |  |  |
| `atr_14` | Nullable(Float64) |  |  |
| `atr_5` | Nullable(Float64) |  |  |
| `ema_10` | Nullable(Float64) |  |  |
| `ema_20` | Nullable(Float64) |  |  |
| `ema_50` | Nullable(Float64) |  |  |
| `volume` | Float64 |  |  |
| `vol_ema_20` | Nullable(Float64) |  |  |
| `vol_spike` | Nullable(Float64) |  |  |
| `z_price_20` | Nullable(Float64) |  |  |
| `z_vol_20` | Nullable(Float64) |  |  |
| `spread_bps` | Nullable(Float64) |  |  |
| `slope_20` | Nullable(Float64) |  |  |
| `r2_20` | Nullable(Float64) |  |  |
| `ver` | UInt64 |  |  |
| `created_at` | DateTime | now() |  |
| `ret_1` | Nullable(Float64) |  | 1-period return |
| `ret_5` | Nullable(Float64) |  | 5-period return |
| `log_ret` | Nullable(Float64) |  | Log return |
| `roc_5` | Nullable(Float64) |  | Rate of change 5-period |
| `roc_10` | Nullable(Float64) |  | Rate of change 10-period |
| `sma_5` | Nullable(Float64) |  | Simple moving average 5-period |
| `sma_20` | Nullable(Float64) |  | Simple moving average 20-period |
| `sma_60` | Nullable(Float64) |  | Simple moving average 60-period |
| `std_20` | Nullable(Float64) |  | Standard deviation 20-period |
| `std_60` | Nullable(Float64) |  | Standard deviation 60-period |
| `z_price_60` | Nullable(Float64) |  | Price z-score 60-period |
| `bb_mid_20` | Nullable(Float64) |  | Bollinger Band middle (20-period SMA) |
| `bb_up_20` | Nullable(Float64) |  | Bollinger Band upper (mid + 2*std) |
| `bb_dn_20` | Nullable(Float64) |  | Bollinger Band lower (mid - 2*std) |
| `bb_bw_20` | Nullable(Float64) |  | Bollinger Band width ratio |
| `bb_pct_20` | Nullable(Float64) |  | Bollinger %B (position within bands) |
| `stoch_k_14` | Nullable(Float64) |  | Stochastic %K (14-period) |
| `stoch_d_3` | Nullable(Float64) |  | Stochastic %D (3-period SMA of %K) |
| `rsi_14` | Nullable(Float64) |  | Relative Strength Index (14-period) |
| `rv_20` | Nullable(Float64) |  | Realized volatility (20-period) |
| `parkinson_20` | Nullable(Float64) |  | Parkinson volatility estimator (20-period) |
| `garman_klass_20` | Nullable(Float64) |  | Garman-Klass volatility estimator (20-period) |
| `donchian_hi_20` | Nullable(Float64) |  | Donchian channel high (20-period) |
| `donchian_lo_20` | Nullable(Float64) |  | Donchian channel low (20-period) |
| `di_plus_14` | Nullable(Float64) |  | Directional Indicator +DI (14-period) |
| `di_minus_14` | Nullable(Float64) |  | Directional Indicator -DI (14-period) |
| `dx_14` | Nullable(Float64) |  | Directional Movement Index (14-period) |
| `adx_14` | Nullable(Float64) |  | Average Directional Index (14-period) |
| `vol_avg_20` | Nullable(Float64) |  | Volume average (20-period) |
| `rvol_roll_20` | Nullable(Float64) |  | Relative volume: current / 20-period avg |
| `rvol_tod_20d` | Nullable(Float64) |  | Relative volume: current / same time-of-day avg (20 days) |
| `cmf_20` | Nullable(Float64) |  | Chaikin Money Flow (20-period) |
| `mfi_14` | Nullable(Float64) |  | Money Flow Index (14-period) |
| `obv_60` | Nullable(Float64) |  | On-Balance Volume (60-period window) |
| `vwap_day` | Nullable(Float64) |  | Intraday VWAP (session cumulative) |
| `vwap_dev_bps` | Nullable(Float64) |  | Deviation from VWAP in basis points |
| `vwap_slope_20` | Nullable(Float64) |  | VWAP trend strength (20-period correlation) |
| `micro_mid` | Nullable(Float64) |  | Microprice mid (from L1) |
| `close_to_mid_bps` | Nullable(Float64) |  | Close price deviation from mid in bps |
| `body` | Nullable(Float64) |  | Candlestick body (close - open) |
| `upper_wick` | Nullable(Float64) |  | Upper wick (high - max(open,close)) |
| `lower_wick` | Nullable(Float64) |  | Lower wick (min(open,close) - low) |
| `range` | Nullable(Float64) |  | High-low range |
| `range_pct` | Nullable(Float64) |  | Range as percentage of close |
| `bar_sec` | UInt16 |  | Bar size in seconds |
| `minute_of_day` | UInt16 |  | Minute of day (0-1440) |
| `open` | Nullable(Float64) |  | Open price |
| `high` | Nullable(Float64) |  | High price |
| `low` | Nullable(Float64) |  | Low price |
| `vwap_bar` | Nullable(Float64) |  | Bar VWAP |
| `trades` | UInt32 |  | Number of trades |
| `vix` | Nullable(Float64) |  |  |
| `vix_sma_20` | Nullable(Float64) |  |  |
| `vix_z_20` | Nullable(Float64) |  |  |
| `vix_chg_1m` | Nullable(Float64) |  |  |
| `vix_chg_5m` | Nullable(Float64) |  |  |
| `vix_rsi_14` | Nullable(Float64) |  |  |

## `fills_live`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toDate(ts)
- ORDER BY (strategy_id, symbol, ts, order_id, fill_uid)
- SETTINGS index_granularity = 4096

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `ts` | DateTime64(3) |  |  |
| `strategy_id` | LowCardinality(String) |  |  |
| `symbol` | LowCardinality(String) |  |  |
| `side` | Enum8('SELL' = -1, 'BUY' = 1) |  |  |
| `qty` | Int64 |  |  |
| `price` | Float64 |  |  |
| `fee` | Float64 | 0 |  |
| `order_id` | String |  |  |
| `decision_id` | String |  |  |
| `param_hash` | FixedString(10) |  |  |
| `liquidity` | LowCardinality(String) | 'UNK' |  |
| `fill_uid` | FixedString(20) |  |  |
| `ver` | UInt64 |  |  |
| `ingest_ts` | DateTime64(3) | now64(3) |  |

## `pnl_ckpt`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toDate(ts_ckpt)
- ORDER BY (strategy_id, symbol, ts_ckpt)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `strategy_id` | LowCardinality(String) |  |  |
| `symbol` | LowCardinality(String) |  |  |
| `ts_ckpt` | DateTime64(3) |  |  |
| `pos` | Int64 |  |  |
| `avg` | Float64 |  |  |
| `cash` | Float64 |  |  |
| `ver` | UInt64 |  |  |

## `pnl_day`
- ENGINE = SummingMergeTree
- PARTITION BY d
- ORDER BY (strategy_id, symbol, d)
- SETTINGS index_granularity = 4096

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `d` | Date |  |  |
| `strategy_id` | LowCardinality(String) |  |  |
| `symbol` | LowCardinality(String) |  |  |
| `sector` | LowCardinality(String) |  |  |
| `traded_shares` | UInt64 |  |  |
| `traded_notional` | Float64 |  |  |
| `realized_pnl` | Float64 |  |  |
| `fees` | Float64 |  |  |
| `upl_close` | Float64 |  |  |
| `nav_close` | Float64 |  |  |
| `cap_used_close` | Float64 |  |  |
| `mkt_volume` | Float64 |  |  |
| `participation` | Float64 |  |  |

## `pnl_fills`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toDate(ts)
- ORDER BY (strategy_id, symbol, ts, fill_uid)
- SETTINGS index_granularity = 4096

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `ts` | DateTime64(3) |  |  |
| `strategy_id` | LowCardinality(String) |  |  |
| `symbol` | LowCardinality(String) |  |  |
| `side` | Int8 |  |  |
| `qty` | Int64 |  |  |
| `price` | Float64 |  |  |
| `fee` | Float64 |  |  |
| `pos_qty_after` | Int64 |  |  |
| `avg_cost_after` | Float64 |  |  |
| `realized_pnl` | Float64 |  |  |
| `cash_after` | Float64 |  |  |
| `order_id` | String |  |  |
| `decision_id` | String |  |  |
| `param_hash` | FixedString(10) |  |  |
| `fill_uid` | FixedString(20) |  |  |
| `ver` | UInt64 |  |  |
| `ingest_ts` | DateTime64(3) | now64(3) |  |

## `pnl_pos_5s`
- ENGINE = ReplacingMergeTree(ver)
- PARTITION BY toDate(ts_end)
- ORDER BY (strategy_id, symbol, ts_end)
- SETTINGS index_granularity = 4096

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `ts_end` | DateTime |  |  |
| `strategy_id` | LowCardinality(String) |  |  |
| `symbol` | LowCardinality(String) |  |  |
| `pos_qty` | Int64 |  |  |
| `avg_cost` | Float64 |  |  |
| `mark_px` | Float64 |  |  |
| `mtm` | Float64 |  |  |
| `upl` | Float64 |  |  |
| `ver` | UInt64 |  |  |
| `ingest_ts` | DateTime64(3) | now64(3) |  |

## `portfolio_snapshots`
- ENGINE = MergeTree
- PARTITION BY toYYYYMMDD(ts)
- ORDER BY (strategy_id, ts, snapshot_id)
- TTL ts + toIntervalDay(180)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `snapshot_id` | String |  |  |
| `ts` | DateTime |  |  |
| `strategy_id` | String |  |  |
| `cash` | Float64 |  |  |
| `equity` | Float64 |  |  |
| `nav` | Float64 |  |  |
| `positions_json` | String |  |  |
| `created_at` | DateTime | now() |  |

## `refs_vix_ticks`
- ENGINE = MergeTree
- ORDER BY ts
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `ts` | DateTime |  |  |
| `vix` | Float64 |  |  |
| `src` | LowCardinality(String) | 'ibkr' |  |

## `ticks_raw`
- ENGINE = ReplacingMergeTree(ingestion_ts)
- PARTITION BY toYYYYMMDD(toDateTime(ts_ns / 1000000000))
- ORDER BY (symbol, ts_ns, type, msg_id)
- TTL toDateTime(ts_ns / 1000000000) + toIntervalDay(90)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `msg_id` | String |  |  |
| `source` | String |  |  |
| `symbol` | String |  |  |
| `ts_ns` | UInt64 |  |  |
| `type` | Enum8('trade' = 1, 'quote' = 2) |  |  |
| `price` | Nullable(Float64) |  |  |
| `size` | Nullable(Float64) |  |  |
| `bid` | Nullable(Float64) |  |  |
| `ask` | Nullable(Float64) |  |  |
| `bid_size` | Nullable(Float64) |  |  |
| `ask_size` | Nullable(Float64) |  |  |
| `exchange` | Nullable(String) |  |  |
| `seq` | Nullable(UInt64) |  |  |
| `ingestion_ts` | DateTime | now() |  |

## `trade_pnl`
- ENGINE = MergeTree
- PARTITION BY toYYYYMMDD(ts)
- ORDER BY (strategy_id, symbol, ts, trade_id)
- TTL ts + toIntervalDay(365)
- SETTINGS index_granularity = 8192

| Column | Type | Default | Comment |
| --- | --- | --- | --- |
| `trade_id` | String |  |  |
| `ts` | DateTime |  |  |
| `strategy_id` | String |  |  |
| `symbol` | String |  |  |
| `side` | Enum8('BUY' = 1, 'SELL' = 2) |  |  |
| `qty` | Float64 |  |  |
| `price` | Float64 |  |  |
| `commission` | Float64 |  |  |
| `realized_pnl` | Nullable(Float64) |  |  |
| `avg_cost` | Nullable(Float64) |  |  |
| `created_at` | DateTime | now() |  |
