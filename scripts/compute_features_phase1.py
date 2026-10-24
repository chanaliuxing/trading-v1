#!/usr/bin/env python3
"""
Feature Engineering Phase 1: Compute 30 New Features
====================================================
Purpose: Calculate and populate 30 new features for trading.features_1m table

Features computed:
- Multi-timeframe (12): 5m/15m/1h aggregations
- Relative strength (8): Performance vs SPY benchmark
- Microstructure (6): Intraday market dynamics
- Time (4): Session position and time-of-day effects

Usage:
    python scripts/compute_features_phase1.py --symbol TSLA --start-date 2024-01-01 --end-date 2024-12-31
    python scripts/compute_features_phase1.py --all-symbols --start-date 2024-01-01
    python scripts/compute_features_phase1.py --symbol TSLA --dry-run  # Preview without writing
"""

import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np
from typing import List, Tuple, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from clickhouse_driver import Client
import structlog

logger = structlog.get_logger()


class Phase1FeatureCalculator:
    """Calculate Phase 1 features (30 new features)"""

    def __init__(self, ch_host: str = 'localhost', ch_port: int = 9000):
        self.client = Client(host=ch_host, port=ch_port)
        self.spy_cache = {}  # Cache SPY data to avoid repeated queries

    def fetch_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch 1-minute bars from ClickHouse"""
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
          AND ts_end >= %(start_date)s
          AND ts_end <= %(end_date)s
          AND bar_sec = 60
        ORDER BY ts_end
        """

        result = self.client.execute(
            query,
            {'symbol': symbol, 'start_date': start_date, 'end_date': end_date}
        )

        if not result:
            logger.warning("no_data", symbol=symbol, start_date=start_date, end_date=end_date)
            return pd.DataFrame()

        df = pd.DataFrame(result, columns=['ts', 'symbol', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'trades'])
        df['ts'] = pd.to_datetime(df['ts'])
        df.set_index('ts', inplace=True)

        return df

    def fetch_spy_data(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch SPY benchmark data"""
        cache_key = f"{start_date}_{end_date}"
        if cache_key in self.spy_cache:
            return self.spy_cache[cache_key]

        df = self.fetch_bars('SPY', start_date, end_date)
        if df.empty:
            logger.warning("spy_data_missing", start_date=start_date, end_date=end_date)
            return pd.DataFrame()

        df = df[['close']].rename(columns={'close': 'spy_close'})
        self.spy_cache[cache_key] = df
        return df

    def compute_multi_timeframe_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute multi-timeframe features (12 features)"""

        # Returns at different timeframes
        df['ret_5m'] = df['close'].pct_change(periods=5)
        df['ret_15m'] = df['close'].pct_change(periods=15)
        df['ret_1h'] = df['close'].pct_change(periods=60)

        # Resample to 5m and 15m for technical indicators
        df_5m = df.resample('5T').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).ffill()

        df_15m = df.resample('15T').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).ffill()

        # RSI on 5m and 15m
        df['rsi_14_5m'] = self._rsi(df_5m['close'], 14).reindex(df.index, method='ffill')
        df['rsi_14_15m'] = self._rsi(df_15m['close'], 14).reindex(df.index, method='ffill')

        # EMA on 5m and 15m
        df['ema_20_5m'] = df_5m['close'].ewm(span=20, adjust=False).mean().reindex(df.index, method='ffill')
        df['ema_20_15m'] = df_15m['close'].ewm(span=20, adjust=False).mean().reindex(df.index, method='ffill')

        # ATR on 5m and 15m
        df['atr_14_5m'] = self._atr(df_5m, 14).reindex(df.index, method='ffill')
        df['atr_14_15m'] = self._atr(df_15m, 14).reindex(df.index, method='ffill')

        # Volume aggregations
        df['volume_5m'] = df['volume'].rolling(5).sum()
        df['volume_15m'] = df['volume'].rolling(15).sum()
        df['volume_1h'] = df['volume'].rolling(60).sum()

        return df

    def compute_relative_strength_features(self, df: pd.DataFrame, spy_df: pd.DataFrame) -> pd.DataFrame:
        """Compute relative strength features vs SPY (8 features)"""

        if spy_df.empty:
            logger.warning("spy_data_empty_skipping_relative_features")
            # Fill with NaN
            for col in ['ret_vs_spy_1m', 'ret_vs_spy_5m', 'ret_vs_spy_15m', 'beta_spy_20',
                        'corr_spy_20', 'relative_volume', 'relative_volatility', 'outperformance_score']:
                df[col] = np.nan
            return df

        # Merge with SPY data
        df = df.join(spy_df, how='left')
        df['spy_close'].ffill(inplace=True)

        # Compute SPY returns
        df['spy_ret_1m'] = df['spy_close'].pct_change(periods=1)
        df['spy_ret_5m'] = df['spy_close'].pct_change(periods=5)
        df['spy_ret_15m'] = df['spy_close'].pct_change(periods=15)

        # Stock returns
        df['stock_ret_1m'] = df['close'].pct_change(periods=1)

        # Relative returns
        df['ret_vs_spy_1m'] = df['stock_ret_1m'] - df['spy_ret_1m']
        df['ret_vs_spy_5m'] = df['ret_5m'] - df['spy_ret_5m']
        df['ret_vs_spy_15m'] = df['ret_15m'] - df['spy_ret_15m']

        # Rolling beta (20 periods)
        df['beta_spy_20'] = df['stock_ret_1m'].rolling(20).cov(df['spy_ret_1m']) / df['spy_ret_1m'].rolling(20).var()

        # Rolling correlation
        df['corr_spy_20'] = df['stock_ret_1m'].rolling(20).corr(df['spy_ret_1m'])

        # Relative volume (assuming we have vol_ema_20 from existing features)
        # This will be populated later if vol_ema_20 exists, otherwise use simple rolling mean
        df['relative_volume'] = df['volume'] / df['volume'].rolling(20).mean()

        # Relative volatility (ATR ratio)
        atr_current = self._atr(df, 14)
        atr_mean_20 = atr_current.rolling(20).mean()
        df['relative_volatility'] = atr_current / atr_mean_20

        # Outperformance score (composite: 0-1 scale)
        # Weighted combination: 40% relative return, 30% correlation, 30% volatility-adjusted return
        ret_score = (df['ret_vs_spy_15m'] + 0.05) / 0.10  # Normalize to 0-1 (assuming ±5% range)
        corr_score = (df['corr_spy_20'] + 1) / 2  # -1 to 1 → 0 to 1
        vol_adj_score = (df['stock_ret_1m'] / atr_current).rolling(20).mean()
        vol_adj_score = (vol_adj_score - vol_adj_score.rolling(100).mean()) / vol_adj_score.rolling(100).std()
        vol_adj_score = (vol_adj_score + 2) / 4  # Normalize

        df['outperformance_score'] = (0.4 * ret_score + 0.3 * corr_score + 0.3 * vol_adj_score).clip(0, 1)

        # Drop intermediate columns
        df.drop(columns=['spy_ret_1m', 'spy_ret_5m', 'spy_ret_15m', 'stock_ret_1m', 'spy_close'], inplace=True)

        return df

    def compute_microstructure_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute microstructure features (6 features)"""

        # VWAP distance
        df['vwap_dist'] = (df['close'] - df['vwap']) / df['vwap']

        # Volume surge (vs 20-period rolling mean)
        vol_mean_20 = df['volume'].rolling(20).mean()
        df['volume_surge'] = df['volume'] / vol_mean_20

        # High-low range
        df['high_low_range'] = (df['high'] - df['low']) / df['close']

        # Intraday volatility (5-period stddev of returns)
        returns = df['close'].pct_change()
        df['intraday_volatility'] = returns.rolling(5).std()

        # Trade intensity (trades per unit volume)
        df['trade_intensity'] = df['trades'] / (df['volume'] + 1)  # +1 to avoid division by zero

        # Momentum strength (5-period price slope)
        df['momentum_strength'] = self._linear_slope(df['close'], 5)

        return df

    def compute_time_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute time-based features (4 features)"""

        # Minute of day (0-1439)
        df['minute_of_day'] = df.index.hour * 60 + df.index.minute

        # Hour of day (0-23)
        df['hour_of_day'] = df.index.hour

        # Session phase
        df['session_phase'] = df['hour_of_day'].apply(self._get_session_phase)

        # Time to close (negative = after hours)
        # Market close at 16:00 ET = 960 minutes from midnight
        market_close_minute = 16 * 60
        df['time_to_close'] = market_close_minute - df['minute_of_day']

        return df

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        """Calculate RSI"""
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 1e-10)
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
        """Calculate linear regression slope over rolling window"""
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
        """Write computed features to ClickHouse"""

        if df.empty:
            logger.info("empty_dataframe_nothing_to_write")
            return 0

        # Select base OHLCV columns + Phase 1 features
        base_cols = ['symbol', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'trades']
        phase1_cols = [
            # Multi-timeframe (12)
            'ret_5m', 'ret_15m', 'ret_1h',
            'rsi_14_5m', 'rsi_14_15m',
            'ema_20_5m', 'ema_20_15m',
            'atr_14_5m', 'atr_14_15m',
            'volume_5m', 'volume_15m', 'volume_1h',
            # Relative strength (8)
            'ret_vs_spy_1m', 'ret_vs_spy_5m', 'ret_vs_spy_15m',
            'beta_spy_20', 'corr_spy_20',
            'relative_volume', 'relative_volatility', 'outperformance_score',
            # Microstructure (6)
            'vwap_dist', 'volume_surge', 'high_low_range',
            'intraday_volatility', 'trade_intensity', 'momentum_strength',
            # Time (4)
            'minute_of_day', 'hour_of_day', 'session_phase', 'time_to_close'
        ]

        all_cols = base_cols + phase1_cols
        df_out = df.reset_index()[['ts'] + all_cols].copy()

        # Replace inf with nan
        df_out.replace([np.inf, -np.inf], np.nan, inplace=True)

        if dry_run:
            logger.info("dry_run_mode_preview", rows=len(df_out), columns=list(df_out.columns))
            print("\n" + "="*60)
            print("DRY RUN - Preview of computed features:")
            print("="*60)
            print(df_out.head(10))
            print(f"\nTotal rows to write: {len(df_out)}")
            print(f"Date range: {df_out['ts'].min()} to {df_out['ts'].max()}")
            print("\nFeature summary (Phase 1 only):")
            print(df_out[phase1_cols].describe())
            return len(df_out)

        # Write to ClickHouse using ALTER UPDATE
        # This is safer than INSERT as it updates existing rows
        query_template = """
        ALTER TABLE trading.features_1m
        UPDATE
            {update_clause}
        WHERE symbol = %(symbol)s
          AND ts >= %(start_ts)s
          AND ts <= %(end_ts)s
        """

        symbol = df_out['symbol'].iloc[0]
        start_ts = df_out['ts'].min()
        end_ts = df_out['ts'].max()

        # Build update clause dynamically
        # Note: ClickHouse ALTER UPDATE is executed, but we need to use INSERT with ON DUPLICATE KEY UPDATE pattern
        # Actually, ClickHouse doesn't support INSERT ... ON DUPLICATE KEY UPDATE
        # We need to use ReplacingMergeTree's natural deduplication

        # Instead, we'll INSERT new rows with the updated values
        # The ReplacingMergeTree will keep the latest version

        insert_query = f"""
        INSERT INTO trading.features_1m
        ({', '.join(['ts'] + all_cols)})
        VALUES
        """

        rows = df_out.to_dict('records')

        logger.info("writing_features", symbol=symbol, rows=len(rows), start_ts=start_ts, end_ts=end_ts)

        # ClickHouse driver doesn't support bulk insert with dict, so we use executemany
        # But first, let's use a simpler approach: batch insert
        self.client.execute(
            f"INSERT INTO trading.features_1m ({', '.join(['ts'] + all_cols)}) VALUES",
            rows
        )

        logger.info("write_complete", symbol=symbol, rows=len(rows))
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
            # Fetch data with extra buffer for rolling calculations
            buffer_days = 30
            buffer_start = (datetime.strptime(start_date, '%Y-%m-%d') - timedelta(days=buffer_days)).strftime('%Y-%m-%d')

            df = self.fetch_bars(symbol, buffer_start, end_date)

            if df.empty:
                logger.warning("no_data_skipping", symbol=symbol)
                return False, 0

            # Fetch SPY data
            spy_df = self.fetch_spy_data(buffer_start, end_date)

            # Compute all features
            logger.info("computing_features", symbol=symbol, rows=len(df))

            df = self.compute_multi_timeframe_features(df)
            df = self.compute_relative_strength_features(df, spy_df)
            df = self.compute_microstructure_features(df)
            df = self.compute_time_features(df)

            # Trim to actual date range (remove buffer)
            df = df[df.index >= start_date]

            # Write to database
            rows_written = self.write_features(df, dry_run=dry_run)

            logger.info("symbol_complete", symbol=symbol, rows=rows_written)
            return True, rows_written

        except Exception as e:
            logger.error("symbol_failed", symbol=symbol, error=str(e), exc_info=True)
            return False, 0


def main():
    parser = argparse.ArgumentParser(description='Compute Phase 1 features (30 new features)')
    parser.add_argument('--symbol', type=str, help='Single symbol to process (e.g., TSLA)')
    parser.add_argument('--all-symbols', action='store_true', help='Process all symbols in database')
    parser.add_argument('--start-date', type=str, required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (YYYY-MM-DD), default: today')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing to database')
    parser.add_argument('--ch-host', type=str, default='localhost', help='ClickHouse host')
    parser.add_argument('--ch-port', type=int, default=9000, help='ClickHouse port')

    args = parser.parse_args()

    if not args.symbol and not args.all_symbols:
        parser.error('Must specify either --symbol or --all-symbols')

    if not args.end_date:
        args.end_date = datetime.now().strftime('%Y-%m-%d')

    calculator = Phase1FeatureCalculator(ch_host=args.ch_host, ch_port=args.ch_port)

    symbols = []
    if args.symbol:
        symbols = [args.symbol]
    elif args.all_symbols:
        # Fetch all unique symbols from bars table
        result = calculator.client.execute(
            "SELECT DISTINCT symbol FROM trading.bars_1m_ibkr WHERE ts_end >= %(start_date)s",
            {'start_date': args.start_date}
        )
        symbols = [row[0] for row in result]
        logger.info("found_symbols", count=len(symbols))

    # Process each symbol
    success_count = 0
    total_rows = 0

    for symbol in symbols:
        success, rows = calculator.process_symbol(
            symbol=symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run
        )

        if success:
            success_count += 1
            total_rows += rows

    logger.info(
        "processing_complete",
        total_symbols=len(symbols),
        success=success_count,
        failed=len(symbols) - success_count,
        total_rows=total_rows
    )

    print("\n" + "="*60)
    print(f"✅ Processing complete:")
    print(f"  - Symbols processed: {success_count}/{len(symbols)}")
    print(f"  - Total rows: {total_rows:,}")
    print(f"  - Date range: {args.start_date} to {args.end_date}")
    if args.dry_run:
        print(f"  - DRY RUN mode (no data written)")
    print("="*60)


if __name__ == '__main__':
    main()
