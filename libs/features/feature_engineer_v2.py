#!/usr/bin/env python3
"""
Enhanced Feature Engineering Module - Phase 1

Adds 50 enhanced features to improve ML model performance:
- Multi-timeframe features (15): Different resolution indicators
- Relative strength features (10): Comparison vs SPY/sector
- Microstructure features (8): Intraday trading dynamics
- Volatility features (7): Multiple volatility measures
- Time features (10): Session timing and day-of-week effects

Target: Improve AUC from 0.515 → 0.53-0.55, IC from 0.034 → 0.045-0.055
"""

from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np


FEATURE_DEPENDENCIES: Dict[str, Dict[str, Any]] = {
    # Class 1: single-asset, 1m-based enhancers
    "ret_5m": {"class": 1, "bases": ["close"], "refs": []},
    "ret_15m": {"class": 1, "bases": ["close"], "refs": []},
    "ret_1h": {"class": 1, "bases": ["close"], "refs": []},
    "vol_5m": {"class": 1, "bases": ["volume"], "refs": []},
    "vol_15m": {"class": 1, "bases": ["volume"], "refs": []},
    "vol_1h": {"class": 1, "bases": ["volume"], "refs": []},
    "realized_vol_5m": {"class": 1, "bases": ["close"], "refs": []},
    "realized_vol_15m": {"class": 1, "bases": ["close"], "refs": []},
    "vwap_dist": {"class": 1, "bases": ["close", "vwap", "atr_14"], "refs": []},
    "vwap_dist_5m": {"class": 1, "bases": ["close", "vwap", "atr_14"], "refs": []},
    "high_low_range": {"class": 1, "bases": ["high", "low", "close"], "refs": []},
    "trade_imb_5m": {"class": 1, "bases": ["open", "close"], "refs": []},
    "volume_surge": {"class": 1, "bases": ["volume"], "refs": []},
    "price_momentum_5m": {"class": 1, "bases": ["close"], "refs": []},
    "reversion_1m": {"class": 1, "bases": ["close"], "refs": []},
    "volatility_ratio": {"class": 1, "bases": ["atr_14", "close"], "refs": []},
    "minute": {"class": 1, "bases": ["ts"], "refs": []},
    "minutes_from_open": {"class": 1, "bases": ["ts"], "refs": []},
    "minutes_to_close": {"class": 1, "bases": ["ts"], "refs": []},
    "is_lunch_time": {"class": 1, "bases": ["ts"], "refs": []},
    "is_monday": {"class": 1, "bases": ["ts"], "refs": []},
    "is_friday": {"class": 1, "bases": ["ts"], "refs": []},
    # Class 2: cross-asset vs SPY (require separate SPY feed)
    "ret_vs_spy_1m": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    "ret_vs_spy_5m": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    "ret_vs_spy_1h": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    "vol_vs_spy": {"class": 2, "bases": ["volume"], "refs": ["SPY:volume"]},
    "corr_spy_20": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    "beta_spy_20": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    "rank_vs_spy": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    "ret_vs_sector": {"class": 2, "bases": ["close"], "refs": ["SPY:close"]},
    # Class 3: reserved for VIX / tick / L2 data (not yet wired)
    # Example placeholders:
    "vix_z_20": {"class": 3, "bases": [], "refs": ["VIX:close"]},
}


class FeatureEngineerV2:
    """Enhanced feature engineering with 50 new features for Phase 1"""

    @staticmethod
    def add_multiframe_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        Multi-timeframe features (15 features)

        Captures price/volume behavior at different time resolutions
        to identify trends and momentum across multiple horizons.
        """
        # Returns at different timeframes
        df['ret_5m'] = df['close'].pct_change(5)
        df['ret_15m'] = df['close'].pct_change(15)
        df['ret_1h'] = df['close'].pct_change(60)

        # Volume at different timeframes
        df['vol_5m'] = df['volume'].rolling(5).mean()
        df['vol_15m'] = df['volume'].rolling(15).mean()
        df['vol_1h'] = df['volume'].rolling(60).mean()

        # RSI at different timeframes
        df['rsi_14_5m'] = FeatureEngineerV2._rolling_rsi(df['close'], 14, 5)
        df['rsi_14_15m'] = FeatureEngineerV2._rolling_rsi(df['close'], 14, 15)

        # EMA calculations
        ema_9 = df['close'].ewm(span=9, adjust=False).mean()
        ema_21 = df['close'].ewm(span=21, adjust=False).mean()
        df['ema_9_5m'] = ema_9.rolling(5).mean()
        df['ema_9_15m'] = ema_9.rolling(15).mean()
        df['ema_cross_1m'] = (ema_9 > ema_21).astype(int)
        df['ema_cross_5m'] = (df['ema_9_5m'] > ema_21.rolling(5).mean()).astype(int)

        # Bollinger Bands
        sma_20 = df['close'].rolling(20).mean()
        std_20 = df['close'].rolling(20).std()
        bb_upper = sma_20 + 2 * std_20
        bb_lower = sma_20 - 2 * std_20
        df['bb_position'] = (df['close'] - bb_lower) / (bb_upper - bb_lower + 1e-8)
        df['bb_width'] = (bb_upper - bb_lower) / (df['close'] + 1e-8)

        # MACD
        ema_12 = df['close'].ewm(span=12, adjust=False).mean()
        ema_26 = df['close'].ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        df['macd_hist'] = macd_line - signal_line

        return df

    @staticmethod
    def add_relative_strength_features(df: pd.DataFrame, spy_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """
        Relative strength features (10 features)

        Measures stock performance relative to market (SPY) to identify
        alpha signals and market-relative momentum.
        """
        if spy_df is None:
            # Fallback: use zeros if SPY not available
            df['ret_vs_spy_1m'] = 0.0
            df['ret_vs_spy_5m'] = 0.0
            df['ret_vs_spy_1h'] = 0.0
            df['vol_vs_spy'] = 1.0
            df['corr_spy_20'] = 0.0
            df['beta_spy_20'] = 1.0
            df['rank_vs_spy'] = 0.5
            df['ret_vs_sector'] = 0.0
            df['volume_rank_20'] = 0.5
            df['price_rank_20'] = 0.5
            return df

        # Align SPY data with stock data. Guard against duplicate indices in
        # SPY data to avoid reindex() errors on large historical windows.
        spy_df = spy_df[~spy_df.index.duplicated(keep='last')]
        spy_aligned = spy_df.reindex(df.index, method='ffill')

        # Calculate SPY returns
        spy_ret_1m = spy_aligned['close'].pct_change()
        spy_ret_5m = spy_aligned['close'].pct_change(5)
        spy_ret_1h = spy_aligned['close'].pct_change(60)

        # Relative returns
        df['ret_vs_spy_1m'] = df['close'].pct_change() - spy_ret_1m
        df['ret_vs_spy_5m'] = df.get('ret_5m', df['close'].pct_change(5)) - spy_ret_5m
        df['ret_vs_spy_1h'] = df.get('ret_1h', df['close'].pct_change(60)) - spy_ret_1h

        # Relative volume
        df['vol_vs_spy'] = df['volume'] / (spy_aligned['volume'] + 1e-8)

        # Correlation with SPY
        df['corr_spy_20'] = df['close'].rolling(20).corr(spy_aligned['close'])

        # Beta: Cov(stock, spy) / Var(spy)
        stock_ret = df['close'].pct_change()
        cov_20 = stock_ret.rolling(20).cov(spy_ret_1m)
        var_spy_20 = spy_ret_1m.rolling(20).var()
        df['beta_spy_20'] = cov_20 / (var_spy_20 + 1e-8)

        # Percentile rank vs SPY
        df['rank_vs_spy'] = (stock_ret > spy_ret_1m).rolling(20).mean()

        # Sector relative (simplified - using SPY as proxy)
        df['ret_vs_sector'] = df['ret_vs_spy_1m']

        # Volume and price percentile ranks
        df['volume_rank_20'] = df['volume'].rolling(20).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) > 0 else 0.5, raw=False
        )
        df['price_rank_20'] = df['close'].rolling(20).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) > 0 else 0.5, raw=False
        )

        return df

    @staticmethod
    def add_microstructure_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        Microstructure features (8 features)

        Captures intraday trading dynamics including VWAP distance,
        volume surges, and mean reversion signals.
        """
        # Ensure vwap exists
        if 'vwap' not in df.columns:
            # Simple VWAP calculation if not present
            df['vwap'] = (df['close'] * df['volume']).rolling(20).sum() / (df['volume'].rolling(20).sum() + 1e-8)

        # Ensure atr_14 exists
        if 'atr_14' not in df.columns:
            # Simple ATR approximation
            high_low = df['high'] - df['low']
            df['atr_14'] = high_low.rolling(14).mean()

        # VWAP distance
        df['vwap_dist'] = (df['close'] - df['vwap']) / (df['atr_14'] + 1e-8)
        df['vwap_dist_5m'] = (df['close'] - df['vwap'].rolling(5).mean()) / (df['atr_14'] + 1e-8)

        # High-low range
        df['high_low_range'] = (df['high'] - df['low']) / (df['close'] + 1e-8)

        # Trade imbalance proxy (close > open indicates buying pressure)
        # Only compute if 'open' column exists. Guard against NaN/None in open
        # by falling back to close when needed.
        if 'open' in df.columns:
            open_filled = df['open'].fillna(df['close'])
            df['trade_imb_5m'] = ((df['close'] > open_filled).astype(int) - 0.5).rolling(5).mean()
        else:
            # Fallback: use close momentum as proxy
            df['trade_imb_5m'] = (df['close'].pct_change() > 0).astype(int) - 0.5

        # Volume surge
        df['volume_surge'] = df['volume'] / (df['volume'].rolling(20).mean() + 1e-8)

        # Price momentum
        df['price_momentum_5m'] = df['close'] / (df['close'].shift(5) + 1e-8) - 1

        # Mean reversion signal
        df['reversion_1m'] = -df['close'].pct_change()

        # Volatility ratio
        atr_50 = df['close'].diff().abs().rolling(50).mean()
        df['volatility_ratio'] = df['atr_14'] / (atr_50 + 1e-8)

        return df

    @staticmethod
    def add_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        Volatility features (7 features)

        Multiple volatility measures including realized volatility,
        ATR percentiles, and range ratios.
        """
        ret_1m = df['close'].pct_change()

        # Realized volatility at different windows (annualized)
        df['realized_vol_5m'] = ret_1m.rolling(5).std() * np.sqrt(252 * 390)
        df['realized_vol_15m'] = ret_1m.rolling(15).std() * np.sqrt(252 * 390)

        # Vol ratio
        df['vol_ratio'] = df['realized_vol_5m'] / (df['realized_vol_15m'] + 1e-8)

        # ATR percentile
        if 'atr_14' in df.columns:
            df['atr_percentile_20'] = df['atr_14'].rolling(20).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) > 0 else 0.5, raw=False
            )
        else:
            df['atr_percentile_20'] = 0.5

        # Range ratio
        high_20 = df['high'].rolling(20).max()
        low_20 = df['low'].rolling(20).min()
        df['range_ratio'] = (df['high'] - df['low']) / (high_20 - low_20 + 1e-8)

        # Close position in daily range
        df['close_position'] = (df['close'] - df['low']) / (df['high'] - df['low'] + 1e-8)

        # Gap from previous close (only if 'open' column exists)
        if 'open' in df.columns:
            df['gap_from_prev'] = (df['open'] - df['close'].shift(1)) / (df['close'].shift(1) + 1e-8)
        else:
            # Fallback: use close-to-close gap
            df['gap_from_prev'] = df['close'].pct_change()

        return df

    @staticmethod
    def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        Time features (10 features)

        Captures session timing effects including time-of-day,
        day-of-week, and special periods (open/close/lunch).
        """
        # Check if index is datetime (skip time features if not)
        if not isinstance(df.index, pd.DatetimeIndex):
            # Return simple placeholder time features
            df['hour'] = 10  # Market hours placeholder
            df['minute'] = 0
            df['minutes_from_open'] = 30
            df['minutes_to_close'] = 360
            df['is_first_30min'] = 0
            df['is_last_30min'] = 0
            df['is_lunch_time'] = 0
            df['day_of_week'] = 3  # Wednesday placeholder
            df['is_power_hour'] = 0
            df['is_pre_market_close'] = 0
            return df

        # Ensure timezone-aware timestamps
        if df.index.tz is None:
            df.index = pd.to_datetime(df.index).tz_localize('UTC')

        # Convert to Eastern Time
        df_et = df.copy()
        df_et.index = df_et.index.tz_convert('America/New_York')

        df['hour'] = df_et.index.hour
        df['minute'] = df_et.index.minute
        df['minutes_from_open'] = (df['hour'] - 9) * 60 + df['minute'] - 30
        df['minutes_to_close'] = (16 - df['hour']) * 60 - df['minute']

        df['is_first_30min'] = (df['minutes_from_open'] < 30).astype(int)
        df['is_last_30min'] = (df['minutes_to_close'] < 30).astype(int)
        df['is_lunch_time'] = ((df['hour'] == 12) | ((df['hour'] == 13) & (df['minute'] == 0))).astype(int)

        df['day_of_week'] = df_et.index.dayofweek + 1  # 1=Mon, 5=Fri
        df['is_monday'] = (df['day_of_week'] == 1).astype(int)
        df['is_friday'] = (df['day_of_week'] == 5).astype(int)

        return df

    @staticmethod
    def add_vix_features(df: pd.DataFrame, vix_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """
        VIX-driven contextual features (currently: simple z-score).

        Expects an optional vix_df with a DateTimeIndex and a single price
        column named 'vix' or 'close'. When provided, we resample it to a
        1-minute grid, align it to df.index, and compute a 20-bar z-score.
        """
        # Default safe fallback: no VIX data wired yet.
        if vix_df is None or vix_df.empty:
            df["vix_z_20"] = 0.0
            return df

        series_col = None
        for c in ("vix", "close"):
            if c in vix_df.columns:
                series_col = c
                break
        if series_col is None:
            df["vix_z_20"] = 0.0
            return df

        v = vix_df[series_col].copy()
        # Ensure datetime index
        if not isinstance(v.index, pd.DatetimeIndex):
            try:
                v.index = pd.to_datetime(v.index)
            except Exception:
                df["vix_z_20"] = 0.0
                return df
        v = v.sort_index()
        # Resample to 1-minute and forward-fill to align with equity bars
        try:
            v_1m = v.resample("1min").mean().ffill()
        except Exception:
            df["vix_z_20"] = 0.0
            return df

        # Align onto df index
        try:
            v_aligned = v_1m.reindex(df.index, method="ffill")
        except Exception:
            df["vix_z_20"] = 0.0
            return df

        roll = v_aligned.rolling(20)
        mean = roll.mean()
        std = roll.std()
        df["vix_z_20"] = (v_aligned - mean) / (std + 1e-8)

        return df

    @staticmethod
    def _rolling_rsi(close: pd.Series, period: int, window: int) -> pd.Series:
        """Calculate rolling RSI"""
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / (loss + 1e-8)
        rsi = 100 - (100 / (1 + rs))
        return rsi.rolling(window).mean()

    @classmethod
    def compute_all_features(
        cls,
        df: pd.DataFrame,
        spy_df: Optional[pd.DataFrame] = None,
        vix_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Compute all 50 enhanced features

        Args:
            df: DataFrame with base features (close, volume, atr_14, etc.)
            spy_df: Optional SPY DataFrame for relative strength features

        Returns:
            DataFrame with all original + 50 new features
        """
        df = df.copy()

        # Basic data-quality guards.
        # Some upstream pipelines may leave occasional NaNs in open/high/low/vwap,
        # especially around partial sessions or instrument onboarding. To prevent
        # entire enhanced feature families from collapsing to NULL, we fall back
        # to close for these columns when values are missing.
        if "close" in df.columns:
            df["close"] = df["close"].astype(float)
        if "volume" in df.columns:
            # Treat missing volume as 0; downstream logic (e.g. relative volume)
            # uses +1e-8 guards to avoid division by zero.
            df["volume"] = df["volume"].fillna(0.0).astype(float)
        for col in ("open", "high", "low", "vwap"):
            if col in df.columns and "close" in df.columns:
                df[col] = df[col].fillna(df["close"])

        # Add all feature groups
        df = cls.add_multiframe_features(df)
        df = cls.add_relative_strength_features(df, spy_df)
        df = cls.add_microstructure_features(df)
        df = cls.add_volatility_features(df)
        df = cls.add_time_features(df)
        df = cls.add_vix_features(df, vix_df)

        return df


# Test/example usage
if __name__ == "__main__":
    from clickhouse_driver import Client

    print("=" * 70)
    print("Enhanced Feature Engineering V2 - Test")
    print("=" * 70)

    # Connect to ClickHouse
    client = Client(host='localhost')

    # Fetch AAPL data
    query = """
        SELECT ts, symbol, open, high, low, close, volume,
               atr_14, z_price_20, slope_20, rsi_14, vwap
        FROM trading.features_1m
        WHERE symbol = 'AAPL'
          AND ts >= '2025-10-01 09:30:00'
          AND ts < '2025-11-05 16:00:00'
        ORDER BY ts
    """

    print("\nFetching AAPL data from ClickHouse...")
    rows = client.execute(query)

    if not rows:
        print("ERROR: No data found for AAPL")
        exit(1)

    df = pd.DataFrame(rows, columns=[
        'ts', 'symbol', 'open', 'high', 'low', 'close', 'volume',
        'atr_14', 'z_price_20', 'slope_20', 'rsi_14', 'vwap'
    ])
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.set_index('ts')

    print(f"Loaded {len(df)} rows")

    # Fetch SPY data
    query_spy = query.replace("'AAPL'", "'SPY'")
    print("Fetching SPY data...")
    rows_spy = client.execute(query_spy)

    if rows_spy:
        spy_df = pd.DataFrame(rows_spy, columns=[
            'ts', 'symbol', 'open', 'high', 'low', 'close', 'volume',
            'atr_14', 'z_price_20', 'slope_20', 'rsi_14', 'vwap'
        ])
        spy_df['ts'] = pd.to_datetime(spy_df['ts'])
        spy_df = spy_df.set_index('ts')
        print(f"Loaded {len(spy_df)} SPY rows")
    else:
        print("WARNING: No SPY data found, using fallback")
        spy_df = None

    # Compute enhanced features
    print("\nComputing enhanced features...")
    eng = FeatureEngineerV2()
    df_enhanced = eng.compute_all_features(df, spy_df)

    print(f"\nOriginal features: {len(df.columns)}")
    print(f"Enhanced features: {len(df_enhanced.columns)}")
    print(f"New features added: {len(df_enhanced.columns) - len(df.columns)}")

    print("\nNew feature columns:")
    new_cols = [c for c in df_enhanced.columns if c not in df.columns]
    for i, col in enumerate(new_cols, 1):
        print(f"  {i:2d}. {col}")

    print(f"\nTotal new features: {len(new_cols)}")

    # Show sample values
    print("\nSample values (last 5 rows):")
    print(df_enhanced[new_cols].tail(5))

    # Check for NaN values
    nan_counts = df_enhanced[new_cols].isna().sum()
    print("\nNaN counts per feature:")
    print(nan_counts[nan_counts > 0])

    # Summary statistics
    print("\nFeature statistics:")
    print(df_enhanced[new_cols].describe())

    print("\n✅ Enhanced feature engineering test complete!")
    print(f"Ready to use for ML training with {len(df_enhanced.columns)} total features")
