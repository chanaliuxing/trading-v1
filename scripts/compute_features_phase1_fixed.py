#!/usr/bin/env python3
"""
Feature Engineering Phase 1: FIX VERSION - Update existing rows in features_1m
===============================================================================
CRITICAL FIX:
- Read from features_1m (has OHLCV from live system)
- Calculate Phase 1 features
- Write back with INCREMENTED ver to ensure ReplacingMergeTree keeps new data

Original script read from bars_1m_ibkr but wrote with ver=0,
causing live data (ver=timestamp) to overwrite backfilled Phase 1 features.
"""

import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np
from typing import List, Tuple, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from clickhouse_driver import Client
import structlog

logger = structlog.get_logger()


class Phase1FeatureCalculatorFixed:
    """Calculate Phase 1 features - FIXED VERSION"""

    def __init__(self, ch_host: str = 'localhost', ch_port: int = 9000):
        self.client = Client(host=ch_host, port=ch_port)
        self.spy_cache = {}

    def fetch_existing_data(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch existing data from features_1m (includes OHLCV from live system)"""
        query = """
        SELECT
            ts,
            symbol,
            open,
            high,
            low,
            close,
            vwap,
            volume,
            trades,
            ver
        FROM trading.features_1m FINAL
        WHERE symbol = %(symbol)s
          AND ts >= %(start_date)s
          AND ts <= %(end_date)s
        ORDER BY ts
        """

        result = self.client.execute(
            query,
            {'symbol': symbol, 'start_date': start_date, 'end_date': end_date}
        )

        if not result:
            logger.warning("no_existing_data", symbol=symbol, start_date=start_date, end_date=end_date)
            return pd.DataFrame()

        df = pd.DataFrame(result, columns=['ts', 'symbol', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'trades', 'ver'])
        df['ts'] = pd.to_datetime(df['ts'])
        df.set_index('ts', inplace=True)

        logger.info("fetched_existing", symbol=symbol, rows=len(df),
                   has_close=len(df[df['close'] > 0]),
                   max_ver=df['ver'].max() if len(df) > 0 else 0)

        return df

    def fetch_spy_data(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch SPY benchmark data from features_1m"""
        cache_key = f"{start_date}_{end_date}"
        if cache_key in self.spy_cache:
            return self.spy_cache[cache_key]

        query = """
        SELECT
            ts,
            close as spy_close
        FROM trading.features_1m FINAL
        WHERE symbol = 'SPY'
          AND ts >= %(start_date)s
          AND ts <= %(end_date)s
          AND close > 0
        ORDER BY ts
        """

        result = self.client.execute(
            query,
            {'start_date': start_date, 'end_date': end_date}
        )

        if not result:
            logger.warning("spy_data_missing", start_date=start_date, end_date=end_date)
            return pd.DataFrame()

        df = pd.DataFrame(result, columns=['ts', 'spy_close'])
        df['ts'] = pd.to_datetime(df['ts'])
        df.set_index('ts', inplace=True)

        self.spy_cache[cache_key] = df
        return df

    def compute_multi_timeframe_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute multi-timeframe features (12 features)"""
        # Returns at different timeframes
        df['ret_5m'] = df['close'].pct_change(periods=5)
        df['ret_15m'] = df['close'].pct_change(periods=15)
        df['ret_1h'] = df['close'].pct_change(periods=60)

        # RSI on different timeframes
        df['rsi_14_5m'] = self._rsi(df['close'], 14).shift(-4)  # 5m = shift back 5 bars
        df['rsi_14_15m'] = self._rsi(df['close'], 14).shift(-14)  # 15m

        # EMA on different timeframes
        df['ema_20_5m'] = df['close'].ewm(span=20).mean().shift(-4)
        df['ema_20_15m'] = df['close'].ewm(span=20).mean().shift(-14)

        # ATR on different timeframes
        df['atr_14_5m'] = self._atr(df, 14).shift(-4)
        df['atr_14_15m'] = self._atr(df, 14).shift(-14)

        # Volume aggregations
        df['volume_5m'] = df['volume'].rolling(5).sum()
        df['volume_15m'] = df['volume'].rolling(15).sum()
        df['volume_1h'] = df['volume'].rolling(60).sum()

        return df

    def compute_relative_strength_features(self, df: pd.DataFrame, spy_df: pd.DataFrame) -> pd.DataFrame:
        """Compute relative strength vs SPY (8 features)"""
        if spy_df.empty:
            logger.warning("spy_missing_skipping_relative_features")
            # Fill with NaN
            for col in ['ret_vs_spy_1m', 'ret_vs_spy_5m', 'ret_vs_spy_15m',
                       'beta_spy_20', 'corr_spy_20',
                       'relative_volume', 'relative_volatility', 'outperformance_score']:
                df[col] = np.nan
            return df

        # Align spy_df with df
        df = df.join(spy_df, how='left')
        df['spy_close'].fillna(method='ffill', inplace=True)

        # Relative returns
        spy_ret_1m = df['spy_close'].pct_change()
        spy_ret_5m = df['spy_close'].pct_change(5)
        spy_ret_15m = df['spy_close'].pct_change(15)

        stock_ret_1m = df['close'].pct_change()
        stock_ret_5m = df['close'].pct_change(5)
        stock_ret_15m = df['close'].pct_change(15)

        df['ret_vs_spy_1m'] = stock_ret_1m - spy_ret_1m
        df['ret_vs_spy_5m'] = stock_ret_5m - spy_ret_5m
        df['ret_vs_spy_15m'] = stock_ret_15m - spy_ret_15m

        # Beta and correlation
        window = 20
        df['beta_spy_20'] = stock_ret_1m.rolling(window).cov(spy_ret_1m) / spy_ret_1m.rolling(window).var()
        df['corr_spy_20'] = stock_ret_1m.rolling(window).corr(spy_ret_1m)

        # Relative metrics
        df['relative_volume'] = df['volume'] / df['volume'].rolling(20).mean()
        stock_atr = self._atr(df, 14)
        df['relative_volatility'] = stock_atr / stock_atr.rolling(20).mean()

        # Outperformance score (0-1)
        df['outperformance_score'] = (
            (df['ret_vs_spy_5m'] > 0).astype(float) * 0.4 +
            (df['ret_vs_spy_15m'] > 0).astype(float) * 0.3 +
            (df['corr_spy_20'] > 0.5).astype(float) * 0.3
        )

        # Remove spy_close column
        df.drop(columns=['spy_close'], inplace=True)

        return df

    def compute_microstructure_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute microstructure features (6 features)"""
        # VWAP distance
        df['vwap_dist'] = (df['close'] - df['vwap']) / df['vwap']

        # Volume surge
        vol_ma = df['volume'].rolling(20).mean()
        df['volume_surge'] = df['volume'] / vol_ma

        # High-low range
        df['high_low_range'] = (df['high'] - df['low']) / df['close']

        # Intraday volatility (5m window)
        returns = df['close'].pct_change()
        df['intraday_volatility'] = returns.rolling(5).std()

        # Trade intensity
        df['trade_intensity'] = df['trades'] / df['volume'].replace(0, np.nan)

        # Momentum strength (5-period slope)
        df['momentum_strength'] = self._linear_slope(df['close'], 5)

        return df

    def compute_time_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute time-based features (4 features)"""
        # Extract from index
        df['minute_of_day'] = df.index.hour * 60 + df.index.minute
        df['hour_of_day'] = df.index.hour

        # Session phase
        df['session_phase'] = df['hour_of_day'].apply(self._get_session_phase)

        # Time to close (minutes, negative = after hours)
        def time_to_close(row):
            if row['hour_of_day'] < 16:
                return (16 * 60) - row['minute_of_day']
            else:
                return -(row['minute_of_day'] - 16 * 60)

        df['time_to_close'] = df.apply(time_to_close, axis=1)

        return df

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        """Calculate RSI"""
        delta = series.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = -delta.where(delta < 0, 0).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate ATR"""
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        return atr

    @staticmethod
    def _linear_slope(series: pd.Series, period: int) -> pd.Series:
        """Calculate linear regression slope"""
        def slope(y):
            if len(y) < period:
                return np.nan
            x = np.arange(len(y))
            return np.polyfit(x, y, 1)[0]
        return series.rolling(period).apply(slope, raw=False)

    @staticmethod
    def _get_session_phase(hour: int) -> str:
        """Determine trading session phase"""
        if hour < 9:
            return 'premarket'
        elif hour == 9 or (hour == 10 and hour < 11):
            return 'open'
        elif hour >= 11 and hour < 15:
            return 'mid'
        elif hour == 15:
            return 'close'
        else:
            return 'after'

    def write_features(self, df: pd.DataFrame, dry_run: bool = False) -> int:
        """Write computed features to ClickHouse with INCREMENTED ver"""

        if df.empty:
            logger.info("empty_dataframe_nothing_to_write")
            return 0

        # Phase 1 feature columns
        phase1_cols = [
            'ret_5m', 'ret_15m', 'ret_1h',
            'rsi_14_5m', 'rsi_14_15m',
            'ema_20_5m', 'ema_20_15m',
            'atr_14_5m', 'atr_14_15m',
            'volume_5m', 'volume_15m', 'volume_1h',
            'ret_vs_spy_1m', 'ret_vs_spy_5m', 'ret_vs_spy_15m',
            'beta_spy_20', 'corr_spy_20',
            'relative_volume', 'relative_volatility', 'outperformance_score',
            'vwap_dist', 'volume_surge', 'high_low_range',
            'intraday_volatility', 'trade_intensity', 'momentum_strength',
            'minute_of_day', 'hour_of_day', 'session_phase', 'time_to_close'
        ]

        # Increment ver: use max(existing_ver) + 1
        max_ver = df['ver'].max() if 'ver' in df.columns else 0
        new_ver = int(max_ver) + 1

        logger.info("writing_with_new_ver", old_ver=max_ver, new_ver=new_ver)

        # Select all columns needed (OHLCV + Phase 1)
        base_cols = ['symbol', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'trades']
        all_cols = base_cols + phase1_cols

        df_out = df.reset_index()[['ts'] + all_cols].copy()
        df_out['ver'] = new_ver  # Critical: Set new version

        # Replace inf with nan
        df_out.replace([np.inf, -np.inf], np.nan, inplace=True)

        if dry_run:
            logger.info("dry_run_mode_preview", rows=len(df_out), columns=list(df_out.columns))
            print("\n" + "="*80)
            print("DRY RUN - Preview:")
            print("="*80)
            print(f"New version: {new_ver}")
            print(df_out[['ts', 'symbol', 'close', 'ret_5m', 'rsi_14_5m', 'ver']].head(10))
            print(f"\nTotal rows: {len(df_out)}")
            print(f"Date range: {df_out['ts'].min()} to {df_out['ts'].max()}")
            return len(df_out)

        # Write to ClickHouse
        # ReplacingMergeTree will keep rows with highest ver
        insert_cols = ['ts'] + all_cols + ['ver']
        rows = df_out[insert_cols].to_dict('records')

        symbol = df_out['symbol'].iloc[0]
        logger.info("writing_features", symbol=symbol, rows=len(rows), ver=new_ver)

        self.client.execute(
            f"INSERT INTO trading.features_1m ({', '.join(insert_cols)}) VALUES",
            rows
        )

        logger.info("write_complete", symbol=symbol, rows=len(rows), ver=new_ver)
        return len(rows)

    def process_symbol(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        dry_run: bool = False
    ) -> Tuple[bool, int]:
        """Process a single symbol"""

        logger.info("processing_symbol", symbol=symbol, start_date=start_date, end_date=end_date)

        try:
            # Fetch existing data with buffer for rolling calculations
            buffer_days = 30
            buffer_start = (datetime.strptime(start_date, '%Y-%m-%d') - timedelta(days=buffer_days)).strftime('%Y-%m-%d')

            df = self.fetch_existing_data(symbol, buffer_start, end_date)

            if df.empty:
                logger.warning("no_existing_data_skipping", symbol=symbol)
                return False, 0

            # Filter to rows with actual OHLCV data
            df = df[df['close'] > 0].copy()

            if df.empty:
                logger.warning("no_ohlcv_data_skipping", symbol=symbol)
                return False, 0

            logger.info("fetched_valid_data", symbol=symbol, rows=len(df))

            # Fetch SPY data
            spy_df = self.fetch_spy_data(buffer_start, end_date)

            # Compute all features
            logger.info("computing_features", symbol=symbol)

            df = self.compute_multi_timeframe_features(df)
            df = self.compute_relative_strength_features(df, spy_df)
            df = self.compute_microstructure_features(df)
            df = self.compute_time_features(df)

            # Trim to actual date range
            df = df[df.index >= start_date]

            logger.info("computed_features", symbol=symbol, final_rows=len(df))

            # Write to database
            rows_written = self.write_features(df, dry_run=dry_run)

            logger.info("symbol_complete", symbol=symbol, rows=rows_written)
            return True, rows_written

        except Exception as e:
            logger.error("symbol_failed", symbol=symbol, error=str(e), exc_info=True)
            return False, 0


def main():
    parser = argparse.ArgumentParser(description='Compute Phase 1 features - FIXED VERSION')
    parser.add_argument('--symbol', type=str, help='Single symbol (e.g., AAPL)')
    parser.add_argument('--start-date', type=str, required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (default: today)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')

    args = parser.parse_args()

    if not args.end_date:
        args.end_date = datetime.now().strftime('%Y-%m-%d')

    print("="*80)
    print("Phase 1 Feature Calculator - FIXED VERSION")
    print("="*80)
    print(f"Symbol: {args.symbol}")
    print(f"Date range: {args.start_date} to {args.end_date}")
    print(f"Dry run: {args.dry_run}")
    print("-"*80)

    calculator = Phase1FeatureCalculatorFixed()

    success, rows = calculator.process_symbol(
        args.symbol,
        args.start_date,
        args.end_date,
        dry_run=args.dry_run
    )

    print("="*80)
    if success:
        print(f"✅ SUCCESS: {rows} rows processed")
    else:
        print(f"❌ FAILED")
        sys.exit(1)


if __name__ == '__main__':
    main()
