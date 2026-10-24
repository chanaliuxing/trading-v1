#!/usr/bin/env python3
"""
Complete Rebuild of features_1m from bars_1m_ibkr
==================================================
Purpose: Rebuild features_1m with 50+ complete features from clean source data

Why needed:
- Current features_1m has 97% missing features (ema_9 only 3% data)
- Source bars_1m_ibkr is 100% complete
- Problem is in broken ETL pipeline

What this does:
1. Fetch raw OHLCV data from bars_1m_ibkr (100% complete)
2. Compute ALL 50+ features in pandas (vectorized, fast)
3. Write to features_1m table (clean rebuild)

Usage:
    # Single symbol
    python pipelines/rebuild_features_1m.py --symbol TSLA --start 2024-01-01 --end 2025-11-06

    # Multiple symbols
    python pipelines/rebuild_features_1m.py --symbols TSLA AAPL NVDA --start 2024-01-01

    # Dry run (preview without writing)
    python pipelines/rebuild_features_1m.py --symbol TSLA --start 2025-10-01 --dry-run

Expected results:
- 120K+ rows per symbol (vs current 5.9K with ema_9)
- 50+ features with 95%+ completeness
- AUC improvement: 0.589 → 0.68+
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from clickhouse_driver import Client

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog
logger = structlog.get_logger()


class FeatureRebuilder:
    """Rebuild features_1m with complete feature set"""

    def __init__(self, host='localhost', port=9000, database='trading'):
        self.client = Client(host=host, port=port)
        self.database = database

    def fetch_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Fetch complete OHLCV bars from bars_1m_ibkr"""
        query = """
        SELECT
            ts_end as ts,
            symbol,
            open,
            high,
            low,
            close,
            vwap,
            volume,
            trades
        FROM trading.bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND ts_end >= %(start)s
          AND ts_end < %(end)s
          AND bar_sec = 60
        ORDER BY ts_end
        """

        logger.info("fetching_bars", symbol=symbol, start=start, end=end)
        result = self.client.execute(query, {'symbol': symbol, 'start': start, 'end': end})

        if not result:
            logger.warning("no_data", symbol=symbol)
            return pd.DataFrame()

        df = pd.DataFrame(result, columns=['ts', 'symbol', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'trades'])
        df['ts'] = pd.to_datetime(df['ts'])
        df = df.set_index('ts')

        logger.info("bars_fetched", rows=len(df), symbol=symbol)
        return df

    def compute_all_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute ALL features in one pass"""
        logger.info("computing_features", total_rows=len(df))

        # Returns (3)
        df['ret_1'] = df['close'].pct_change()
        df['ret_5'] = df['close'].pct_change(5)
        df['log_ret'] = np.log(df['close'] / df['close'].shift(1))

        # Rate of change (2)
        df['roc_5'] = df['close'].pct_change(5)
        df['roc_10'] = df['close'].pct_change(10)

        # Moving averages - SMA (6)
        for period in [5, 20, 60]:
            df[f'sma_{period}'] = df['close'].rolling(period).mean()

        # Moving averages - EMA (7)
        for period in [9, 10, 20, 21, 50]:
            df[f'ema_{period}'] = df['close'].ewm(span=period, adjust=False).mean()

        # EMA cross signal
        df['ema_cross'] = (df['ema_9'] > df['ema_21']).astype(int)

        # Volatility - Rolling std (2)
        df['std_20'] = df['close'].rolling(20).std()
        df['std_60'] = df['close'].rolling(60).std()

        # Volatility - ATR (3)
        df['atr_5'] = self._compute_atr(df, 5)
        df['atr_14'] = self._compute_atr(df, 14)
        df['atr_20'] = self._compute_atr(df, 20)

        # Volatility - Advanced (3)
        df['rv_20'] = df['ret_1'].rolling(20).std() * np.sqrt(252)  # Realized volatility
        df['parkinson_20'] = self._compute_parkinson_vol(df, 20)
        df['garman_klass_20'] = self._compute_gk_vol(df, 20)

        # Bollinger Bands (4)
        df['bb_mid_20'] = df['close'].rolling(20).mean()
        std_20 = df['close'].rolling(20).std()
        df['bb_up_20'] = df['bb_mid_20'] + 2 * std_20
        df['bb_dn_20'] = df['bb_mid_20'] - 2 * std_20
        df['bb_bw_20'] = (df['bb_up_20'] - df['bb_dn_20']) / df['bb_mid_20']
        df['bb_pct_20'] = (df['close'] - df['bb_dn_20']) / (df['bb_up_20'] - df['bb_dn_20'])

        # RSI (1)
        df['rsi_14'] = self._compute_rsi(df['close'], 14)

        # Stochastic (2)
        df['stoch_k_14'], df['stoch_d_3'] = self._compute_stochastic(df, 14, 3)

        # Donchian channels (2)
        df['donchian_hi_20'] = df['high'].rolling(20).max()
        df['donchian_lo_20'] = df['low'].rolling(20).min()

        # ADX (1)
        df['adx_14'] = self._compute_adx(df, 14)

        # Volume features (3)
        df['vol_ema_20'] = df['volume'].ewm(span=20, adjust=False).mean()
        df['vol_spike'] = df['volume'] / df['vol_ema_20']
        df['cmf_20'] = self._compute_cmf(df, 20)
        df['mfi_14'] = self._compute_mfi(df, 14)
        df['obv_60'] = self._compute_obv(df)

        # Price-related (6)
        df['z_price_20'] = (df['close'] - df['close'].rolling(20).mean()) / df['close'].rolling(20).std()
        df['z_vol_20'] = (df['volume'] - df['volume'].rolling(20).mean()) / df['volume'].rolling(20).std()
        df['slope_20'] = self._compute_slope(df['close'], 20)
        df['r2_20'] = self._compute_r2(df['close'], 20)
        df['vwap_dev_bps'] = (df['close'] - df['vwap']) / df['vwap'] * 10000

        # Z-scores (4)
        df['zscore_close_20'] = (df['close'] - df['close'].rolling(20).mean()) / df['close'].rolling(20).std()
        df['zscore_volume_20'] = (df['volume'] - df['volume'].rolling(20).mean()) / df['volume'].rolling(20).std()
        df['zscore_close_60'] = (df['close'] - df['close'].rolling(60).mean()) / df['close'].rolling(60).std()
        df['zscore_volume_60'] = (df['volume'] - df['volume'].rolling(60).mean()) / df['volume'].rolling(60).std()

        # Statistical moments (4)
        df['skew_20'] = df['ret_1'].rolling(20).skew()
        df['kurt_20'] = df['ret_1'].rolling(20).kurt()
        df['autocorr_ret_1'] = df['ret_1'].rolling(20).apply(lambda x: x.autocorr(lag=1), raw=False)

        # Time features (2)
        df['minute_of_day'] = df.index.hour * 60 + df.index.minute
        # Time since market open (clip to 0-390 for regular trading hours)
        df['time_since_open'] = (df['minute_of_day'] - 570).clip(lower=0, upper=65535).astype('int32')  # 9:30 AM = 570 min
        df['is_first_30min'] = (df['time_since_open'] <= 30).astype(int)
        df['is_last_30min'] = (df['time_since_open'] >= 360).astype(int)

        # --- New multi-horizon volatility features (vol_5m/15m/1h) ---
        # Use rolling std of 1-bar returns over different horizons.
        df['vol_5m'] = df['ret_1'].rolling(5).std()
        df['vol_15m'] = df['ret_1'].rolling(15).std()
        df['vol_1h'] = df['ret_1'].rolling(60).std()
        df['realized_vol_15m'] = df['vol_15m'] * np.sqrt(252)

        # Volume rank over the last 20 bars (0..1)
        df['volume_rank_20'] = df['volume'].rolling(20).rank(pct=True)

        # VWAP distance over a 5-bar window: deviation of close from
        # 5-bar volume-weighted average price.
        roll_vol = df['volume'].rolling(5).sum()
        roll_pv = (df['close'] * df['volume']).rolling(5).sum()
        vwap_5m = roll_pv / roll_vol
        df['vwap_dist_5m'] = (df['close'] - vwap_5m) / vwap_5m

        # Additional time/calendar features to populate existing schema:
        # minute (0-59), minutes_from_open, minutes_to_close, and simple
        # weekday/lunch flags.
        df['minute'] = df.index.minute.astype('int8')
        df['minutes_from_open'] = (df['minute_of_day'] - 570).astype('int16')
        df['minutes_to_close'] = (960 - df['minute_of_day']).astype('int16')  # 16:00 = 960
        dow = df.index.dayofweek
        df['is_monday'] = (dow == 0).astype('int8')
        df['is_friday'] = (dow == 4).astype('int8')
        # Treat 12:00-13:30 as "lunch" session for intraday patterns.
        df['is_lunch_time'] = ((df['minute_of_day'] >= 720) & (df['minute_of_day'] <= 810)).astype('int8')

        logger.info("features_computed", total_features=len(df.columns))
        return df

    def compute_spy_ret5(self, spy_df: pd.DataFrame) -> pd.Series:
        """
        Compute SPY 5-bar return for cross-sectional alignment

        Args:
            spy_df: DataFrame with SPY bars (indexed by ts)

        Returns:
            Series of spy_ret_5 (indexed by ts)
        """
        if spy_df.empty:
            return pd.Series(dtype=float)

        spy_ret5 = spy_df['close'].pct_change(5)
        spy_ret5.name = 'spy_ret_5'

        logger.info("spy_ret5_computed", non_null_count=spy_ret5.notna().sum(), total=len(spy_ret5))
        return spy_ret5

    def _compute_atr(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Average True Range"""
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _compute_parkinson_vol(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Parkinson volatility estimator"""
        hl = np.log(df['high'] / df['low'])
        return np.sqrt(hl.rolling(period).mean() / (4 * np.log(2))) * np.sqrt(252)

    def _compute_gk_vol(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Garman-Klass volatility estimator"""
        hl = (np.log(df['high'] / df['low'])) ** 2
        co = (np.log(df['close'] / df['open'])) ** 2
        return np.sqrt(0.5 * hl.rolling(period).mean() - (2 * np.log(2) - 1) * co.rolling(period).mean()) * np.sqrt(252)

    def _compute_rsi(self, series: pd.Series, period: int) -> pd.Series:
        """Relative Strength Index"""
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _compute_stochastic(self, df: pd.DataFrame, k_period: int, d_period: int) -> tuple:
        """Stochastic oscillator"""
        low_min = df['low'].rolling(k_period).min()
        high_max = df['high'].rolling(k_period).max()
        k = 100 * (df['close'] - low_min) / (high_max - low_min)
        d = k.rolling(d_period).mean()
        return k, d

    def _compute_adx(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Average Directional Index"""
        # Simplified ADX (full implementation is complex)
        plus_dm = df['high'].diff()
        minus_dm = -df['low'].diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm < 0] = 0

        tr = self._compute_atr(df, 1)
        plus_di = 100 * (plus_dm.rolling(period).mean() / tr.rolling(period).mean())
        minus_di = 100 * (minus_dm.rolling(period).mean() / tr.rolling(period).mean())

        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx.rolling(period).mean()

    def _compute_cmf(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Chaikin Money Flow"""
        mf_multiplier = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'])
        mf_volume = mf_multiplier * df['volume']
        return mf_volume.rolling(period).sum() / df['volume'].rolling(period).sum()

    def _compute_mfi(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Money Flow Index"""
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        money_flow = typical_price * df['volume']

        positive_flow = money_flow.where(typical_price > typical_price.shift(), 0).rolling(period).sum()
        negative_flow = money_flow.where(typical_price < typical_price.shift(), 0).rolling(period).sum()

        mfi_ratio = positive_flow / negative_flow
        return 100 - (100 / (1 + mfi_ratio))

    def _compute_obv(self, df: pd.DataFrame) -> pd.Series:
        """On Balance Volume"""
        obv = pd.Series(index=df.index, dtype=float)
        obv.iloc[0] = df['volume'].iloc[0]

        for i in range(1, len(df)):
            if df['close'].iloc[i] > df['close'].iloc[i-1]:
                obv.iloc[i] = obv.iloc[i-1] + df['volume'].iloc[i]
            elif df['close'].iloc[i] < df['close'].iloc[i-1]:
                obv.iloc[i] = obv.iloc[i-1] - df['volume'].iloc[i]
            else:
                obv.iloc[i] = obv.iloc[i-1]

        return obv.rolling(60).mean()

    def _compute_slope(self, series: pd.Series, period: int) -> pd.Series:
        """Linear regression slope"""
        def calc_slope(y):
            if len(y) < 2:
                return np.nan
            x = np.arange(len(y))
            slope = np.polyfit(x, y, 1)[0]
            return slope

        return series.rolling(period).apply(calc_slope, raw=True)

    def _compute_r2(self, series: pd.Series, period: int) -> pd.Series:
        """R-squared of linear fit"""
        def calc_r2(y):
            if len(y) < 2:
                return np.nan
            x = np.arange(len(y))
            slope, intercept = np.polyfit(x, y, 1)
            y_pred = slope * x + intercept
            ss_res = np.sum((y - y_pred) ** 2)
            ss_tot = np.sum((y - np.mean(y)) ** 2)
            return 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

        return series.rolling(period).apply(calc_r2, raw=True)

    def write_to_clickhouse(self, df: pd.DataFrame, symbol: str, dry_run: bool = False):
        """Write features to ClickHouse"""
        df = df.reset_index()
        df['symbol'] = symbol
        df['ver'] = 1
        df['created_at'] = datetime.now()

        # Select columns that exist in features_1m table
        columns = [
            'symbol', 'ts', 'close', 'vwap', 'atr_14', 'atr_5', 'ema_10', 'ema_20',
            'ema_50', 'volume', 'vol_ema_20', 'vol_spike', 'z_price_20', 'z_vol_20',
            'slope_20', 'r2_20', 'ver', 'created_at', 'ret_1', 'ret_5', 'log_ret',
            'roc_5', 'roc_10', 'sma_5', 'sma_20', 'sma_60', 'std_20', 'std_60',
            'bb_mid_20', 'bb_up_20', 'bb_dn_20', 'bb_bw_20', 'bb_pct_20',
            'stoch_k_14', 'stoch_d_3', 'rsi_14', 'rv_20', 'parkinson_20',
            'garman_klass_20', 'donchian_hi_20', 'donchian_lo_20', 'adx_14',
            'cmf_20', 'mfi_14', 'obv_60', 'vwap_dev_bps', 'minute_of_day',
            'zscore_close_20', 'zscore_volume_20', 'skew_20', 'kurt_20',
            'autocorr_ret_1', 'time_since_open', 'is_first_30min', 'is_last_30min',
            'ema_9', 'ema_21', 'ema_cross', 'atr_20', 'zscore_close_60',
            'zscore_volume_60',
            # newly populated multi-horizon volatility features
            'vol_5m', 'vol_15m', 'vol_1h', 'realized_vol_15m',
            # volume-based rank and VWAP distance
            'volume_rank_20', 'vwap_dist_5m',
            # cross-sectional market features
            'spy_ret_5',
            # additional time/calendar features
            'minute', 'minutes_from_open', 'minutes_to_close',
            'is_lunch_time', 'is_monday', 'is_friday',
        ]

        # Keep only columns that exist in df
        available_cols = [col for col in columns if col in df.columns]
        df_write = df[available_cols].copy()

        # Replace NaN with None for ClickHouse
        df_write = df_write.where(pd.notnull(df_write), None)

        if dry_run:
            logger.info("dry_run_preview", rows=len(df_write), columns=len(available_cols))
            print(f"\n✓ Dry run - would write {len(df_write):,} rows with {len(available_cols)} features")
            print(f"  Sample features: {', '.join(available_cols[:10])}")
            print(f"  Date range: {df_write['ts'].min()} to {df_write['ts'].max()}")
            return

        logger.info("writing_to_clickhouse", rows=len(df_write), symbol=symbol)

        # Delete existing data for this symbol and date range
        min_ts = df_write['ts'].min()
        max_ts = df_write['ts'].max()
        self.client.execute(f"""
            ALTER TABLE {self.database}.features_1m
            DELETE WHERE symbol = '{symbol}'
              AND ts >= '{min_ts}'
              AND ts <= '{max_ts}'
        """)

        # Insert new data in batches to avoid partition limit
        data = [tuple(row) for row in df_write.values]
        batch_size = 10000  # Insert 10K rows at a time
        total_rows = len(data)

        for i in range(0, total_rows, batch_size):
            batch = data[i:i + batch_size]
            self.client.execute(
                f"INSERT INTO {self.database}.features_1m ({', '.join(available_cols)}) VALUES",
                batch
            )
            logger.info("batch_written", batch_num=i//batch_size + 1, rows=len(batch))
            print(f"  Batch {i//batch_size + 1}/{(total_rows + batch_size - 1)//batch_size}: {len(batch)} rows")

        logger.info("write_complete", rows=total_rows, symbol=symbol)
        print(f"✓ Wrote {total_rows:,} rows for {symbol}")


def main():
    parser = argparse.ArgumentParser(description="Rebuild features_1m from bars_1m_ibkr")
    parser.add_argument('--symbol', help="Single symbol to rebuild")
    parser.add_argument('--symbols', nargs='+', help="Multiple symbols")
    parser.add_argument('--start', required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument('--end', help="End date (default: today)")
    parser.add_argument('--dry-run', action='store_true', help="Preview without writing")
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=9000)

    args = parser.parse_args()

    if not args.symbol and not args.symbols:
        parser.error("Must specify either --symbol or --symbols")

    symbols = [args.symbol] if args.symbol else args.symbols
    end_date = args.end or datetime.now().strftime('%Y-%m-%d')

    print("="*60)
    print("FEATURES_1M REBUILD")
    print("="*60)
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Date range: {args.start} to {end_date}")
    print(f"Dry run: {args.dry_run}")
    print()

    rebuilder = FeatureRebuilder(host=args.host, port=args.port)

    # Step 1: Fetch SPY data for cross-sectional features (spy_ret_5)
    print(f"\n📈 Fetching SPY data for market context...")
    spy_df = rebuilder.fetch_bars("SPY", args.start, end_date)
    spy_ret5 = None
    if spy_df.empty:
        print(f"⚠️  No SPY data; spy_ret_5 will remain NULL for all symbols")
    else:
        spy_ret5 = rebuilder.compute_spy_ret5(spy_df)
        print(f"✓ SPY data loaded: {len(spy_ret5):,} bars, {spy_ret5.notna().sum():,} non-null spy_ret_5 values")

    # Step 2: Process each symbol
    for symbol in symbols:
        print(f"\n📊 Processing {symbol}...")

        # Fetch bars
        df = rebuilder.fetch_bars(symbol, args.start, end_date)
        if df.empty:
            print(f"❌ No data for {symbol}")
            continue

        # Compute features
        df = rebuilder.compute_all_features(df)

        # Align SPY ret_5 to this symbol's timestamps
        if spy_ret5 is not None and not spy_ret5.empty:
            # Use index (ts) to align; missing timestamps will get NaN → NULL in ClickHouse
            aligned_spy = spy_ret5.reindex(df.index)
            df['spy_ret_5'] = aligned_spy
            non_null_count = df['spy_ret_5'].notna().sum()
            coverage_pct = (non_null_count / len(df)) * 100 if len(df) > 0 else 0
            print(f"  ✓ spy_ret_5 aligned: {non_null_count:,}/{len(df):,} ({coverage_pct:.1f}%) non-null")
        else:
            df['spy_ret_5'] = np.nan
            print(f"  ⚠️  spy_ret_5 set to NULL (no SPY data)")

        # Write to ClickHouse
        rebuilder.write_to_clickhouse(df, symbol, dry_run=args.dry_run)

    print("\n" + "="*60)
    print("✓ REBUILD COMPLETE")
    print("="*60)

    if not args.dry_run:
        print("\nNext steps:")
        print("1. Generate labels:")
        print(f"   python pipelines/generate_labels.py --symbols {' '.join(symbols)}")
        print("\n2. Train models:")
        print(f"   python pipelines/optimize_xgb_optuna.py --symbol {symbols[0]} --n-trials 100")


if __name__ == '__main__':
    main()
