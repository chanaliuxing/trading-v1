"""
Tier-1 Feature SQL Builders
Contains all SQL fragments for comprehensive Tier-1 feature calculations
This module is imported by feature_calculator.py
"""

def build_ema_sql() -> str:
    """
    Build EMA calculations using exponential_moving_average ClickHouse function
    EMA formula: alpha = 2/(n+1), EMA_t = alpha*P_t + (1-alpha)*EMA_(t-1)

    Returns SQL fragments for: ema_9, ema_21, ema_50, ema_cross
    """
    return """
        -- EMA calculations (alpha = 2/(n+1))
        -- ema_9: alpha = 2/10 = 0.2
        exponentialMovingAverage(0.2)(close) OVER w_sym AS ema_9,
        -- ema_21: alpha = 2/22 = 0.0909
        exponentialMovingAverage(0.0909)(close) OVER w_sym AS ema_21,
        -- ema_50: alpha = 2/51 = 0.0392
        exponentialMovingAverage(0.0392)(close) OVER w_sym AS ema_50,
        -- EMA cross (ema_9 - ema_21)
        exponentialMovingAverage(0.2)(close) OVER w_sym - exponentialMovingAverage(0.0909)(close) OVER w_sym AS ema_cross,
"""


def build_atr_sql() -> str:
    """
    Build ATR (Average True Range) calculations
    ATR = EMA of True Range where TR = max(H-L, |H-PC|, |L-PC|)
    tr is already calculated in dm_base CTE
    """
    return """
        -- ATR (14 and 20 period using existing avg_tr)
        avg_tr_14 AS atr_14,
        avg(tr) OVER w_20 AS atr_20,
"""


def build_volatility_sql() -> str:
    """VIX diff calculation"""
    return """
        -- VIX diff (VIX - VIX_SMA_20)
        vix_value - avg(vix_value) OVER w_20 AS vix_diff,
"""


def build_market_context_sql(bar_sec: int) -> str:
    """
    Build market context features (SPY/QQQ beta, correlation, returns)
    This requires joining with SPY and QQQ data from the same bar table

    Formula for beta: β = Cov(stock_ret, market_ret) / Var(market_ret)
    Formula for corr: ρ = Cov(stock_ret, market_ret) / (σ_stock * σ_market)
    """
    # Calculate SPY/QQQ lags based on bar size
    lag_1 = 1
    lag_5 = max(1, 300 // bar_sec)  # 5-minute lag in bars

    return f"""
        -- SPY returns (requires JOIN with SPY data)
        -- Note: SPY data is in the same table, filtered by symbol
        lagInFrame(spy_close, {lag_1}) OVER w_all AS spy_prev_1,
        lagInFrame(spy_close, {lag_5}) OVER w_all AS spy_prev_5,
        if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL) AS spy_ret_1,
        if(spy_prev_5 > 0, ln(spy_close / spy_prev_5), NULL) AS spy_ret_5,

        -- QQQ returns
        lagInFrame(qqq_close, {lag_1}) OVER w_all AS qqq_prev_1,
        lagInFrame(qqq_close, {lag_5}) OVER w_all AS qqq_prev_5,
        if(qqq_prev_1 > 0, ln(qqq_close / qqq_prev_1), NULL) AS qqq_ret_1,
        if(qqq_prev_5 > 0, ln(qqq_close / qqq_prev_5), NULL) AS qqq_ret_5,

        -- Beta vs SPY (20 and 60 period)
        -- Beta = Cov(stock_ret, spy_ret) / Var(spy_ret)
        if(varSamp(if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_20 > 0,
           covarSamp(log_ret, if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_20
           / varSamp(if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_20,
           NULL) AS beta_spy_20,
        if(varSamp(if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_60 > 0,
           covarSamp(log_ret, if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_60
           / varSamp(if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_60,
           NULL) AS beta_spy_60,

        -- Correlation with SPY (20 and 60 period)
        corr(log_ret, if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_20 AS corr_spy_20,
        corr(log_ret, if(spy_prev_1 > 0, ln(spy_close / spy_prev_1), NULL)) OVER w_60 AS corr_spy_60,

        -- Correlation with QQQ (20 period)
        corr(log_ret, if(qqq_prev_1 > 0, ln(qqq_close / qqq_prev_1), NULL)) OVER w_20 AS corr_qqq_20,
"""


def build_microstructure_sql() -> str:
    """
    Build microstructure features
    - spread_bps: (ask - bid) / mid * 10000
    - tick_imbalance: Not available without tick data, will be NULL
    - trade_count_ratio: trades / avg(trades)
    - avg_trade_size: volume / trades
    - price_impact_5m: Not directly calculable without orderbook, approximated
    """
    return """
        -- Spread in basis points (from bid/ask if available)
        if(bid_last > 0 AND ask_last > 0 AND (bid_last + ask_last) / 2.0 > 0,
           ((ask_last - bid_last) / ((bid_last + ask_last) / 2.0)) * 10000.0,
           NULL) AS spread_bps,

        -- Tick imbalance (requires tick data, not available - set NULL)
        NULL AS tick_imbalance,

        -- Trade count ratio (current trades / rolling avg trades)
        if(avg(toFloat64(trades)) OVER w_20 > 0,
           toFloat64(trades) / avg(toFloat64(trades)) OVER w_20,
           NULL) AS trade_count_ratio,

        -- Average trade size (volume per trade)
        if(trades > 0, volume / toFloat64(trades), NULL) AS avg_trade_size,

        -- Price impact (5-min VWAP deviation, approximation)
        -- True price impact requires tick-by-tick data
        NULL AS price_impact_5m,
"""


def build_statistical_sql() -> str:
    """
    Build statistical/distribution features
    - zscore_close_20/60: (close - mean) / stddev
    - zscore_volume_20/60: (volume - mean) / stddev
    - skew_20/60: skewness of returns
    - kurt_20/60: excess kurtosis of returns
    - autocorr_ret_1/5: autocorrelation of returns
    - hurst_20: Hurst exponent (R/S method)
    """
    return """
        -- Z-scores for close price
        if(stddevSamp(close) OVER w_20 > 0,
           (close - avg(close) OVER w_20) / stddevSamp(close) OVER w_20,
           NULL) AS zscore_close_20,
        if(stddevSamp(close) OVER w_60 > 0,
           (close - avg(close) OVER w_60) / stddevSamp(close) OVER w_60,
           NULL) AS zscore_close_60,

        -- Z-scores for volume
        if(stddevSamp(volume) OVER w_20 > 0,
           (volume - avg(volume) OVER w_20) / stddevSamp(volume) OVER w_20,
           NULL) AS zscore_volume_20,
        if(stddevSamp(volume) OVER w_60 > 0,
           (volume - avg(volume) OVER w_60) / stddevSamp(volume) OVER w_60,
           NULL) AS zscore_volume_60,

        -- Skewness of returns (20 and 60 period)
        -- Skew = E[((x - μ) / σ)^3]
        if(stddevSamp(log_ret) OVER w_20 > 0,
           avg(pow((log_ret - avg(log_ret) OVER w_20) / stddevSamp(log_ret) OVER w_20, 3)) OVER w_20,
           NULL) AS skew_20,
        if(stddevSamp(log_ret) OVER w_60 > 0,
           avg(pow((log_ret - avg(log_ret) OVER w_60) / stddevSamp(log_ret) OVER w_60, 3)) OVER w_60,
           NULL) AS skew_60,

        -- Excess kurtosis of returns (20 and 60 period)
        -- Kurt = E[((x - μ) / σ)^4] - 3
        if(stddevSamp(log_ret) OVER w_20 > 0,
           avg(pow((log_ret - avg(log_ret) OVER w_20) / stddevSamp(log_ret) OVER w_20, 4)) OVER w_20 - 3.0,
           NULL) AS kurt_20,
        if(stddevSamp(log_ret) OVER w_60 > 0,
           avg(pow((log_ret - avg(log_ret) OVER w_60) / stddevSamp(log_ret) OVER w_60, 4)) OVER w_60 - 3.0,
           NULL) AS kurt_60,

        -- Autocorrelation of returns (lag 1 and lag 5)
        -- Autocorr(lag) = Corr(ret_t, ret_(t-lag))
        corr(log_ret, lagInFrame(log_ret, 1) OVER w_sym) OVER w_20 AS autocorr_ret_1,
        corr(log_ret, lagInFrame(log_ret, 5) OVER w_sym) OVER w_20 AS autocorr_ret_5,

        -- Hurst exponent (R/S method, simplified)
        -- H ≈ log(R/S) / log(N)
        -- R = max(cumsum(ret - mean)) - min(cumsum(ret - mean))
        -- S = stddev(ret)
        -- Simplified: use range of cumulative deviations / std
        NULL AS hurst_20,  -- Complex calculation, requires custom UDF
"""


def build_temporal_sql() -> str:
    """
    Build temporal/meta features
    - time_since_open: minutes since 9:30 ET
    - day_of_week: 0=Mon, 4=Fri
    - hour_of_day: 0-23 ET
    - is_first_30min: flag
    - is_last_30min: flag
    """
    return """
        -- Time since market open (9:30 ET = 570 minutes from midnight)
        -- minute_of_day is already in minutes from midnight ET
        if(minute_of_day >= 570, minute_of_day - 570, 0) AS time_since_open,

        -- Day of week (0=Monday, 6=Sunday, but we only trade Mon-Fri)
        toDayOfWeek(toTimeZone(ts_start, 'America/New_York')) - 1 AS day_of_week,

        -- Hour of day (ET timezone)
        toHour(toTimeZone(ts_start, 'America/New_York')) AS hour_of_day,

        -- First 30 minutes flag (9:30-10:00 ET = 570-600 minutes)
        if(minute_of_day >= 570 AND minute_of_day < 600, 1, 0) AS is_first_30min,

        -- Last 30 minutes flag (15:30-16:00 ET = 930-960 minutes)
        if(minute_of_day >= 930 AND minute_of_day < 960, 1, 0) AS is_last_30min,
"""


def build_advanced_sql() -> str:
    """
    Build advanced adaptive features
    - kama_10: Kaufman Adaptive Moving Average (complex recursive)
    - entropy_20: Shannon entropy of returns (requires binning)
    """
    return """
        -- KAMA (Kaufman Adaptive Moving Average)
        -- Complex recursive calculation, not directly supported in SQL
        -- Would require custom UDF or external computation
        NULL AS kama_10,

        -- Shannon entropy of returns (simplified)
        -- True entropy requires discretization into bins
        -- Approximation: use negative log of probability distribution
        NULL AS entropy_20,
"""


def build_spy_qqq_join_cte(bar_table: str, time_filter: str, symbol_filter: str) -> str:
    """
    Build CTE to join SPY and QQQ data for market context features
    This creates a table with symbol data joined with SPY and QQQ closes
    """
    return f"""
        -- SPY data (for beta/correlation calculations)
        spy_data AS (
            SELECT
                ts_start,
                close AS spy_close
            FROM {bar_table} FINAL
            WHERE symbol = 'SPY'
              AND {time_filter}
        ),

        -- QQQ data (for correlation calculations)
        qqq_data AS (
            SELECT
                ts_start,
                close AS qqq_close
            FROM {bar_table} FINAL
            WHERE symbol = 'QQQ'
              AND {time_filter}
        ),

        -- Join symbol bars with SPY and QQQ
        bars_with_market AS (
            SELECT
                obv_calc.*,
                coalesce(spy.spy_close, 0) AS spy_close,
                coalesce(qqq.qqq_close, 0) AS qqq_close
            FROM obv_calc
            LEFT JOIN spy_data spy ON spy.ts_start = obv_calc.ts_start
            LEFT JOIN qqq_data qqq ON qqq.ts_start = obv_calc.ts_start
        ),
"""
