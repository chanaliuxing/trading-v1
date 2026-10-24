#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feature Computation Script - Compute technical indicators from bars data

This script reads bars from ClickHouse (bars_1m_ibkr, bars_5m_ibkr) and computes
technical features needed for backtesting.

Usage:
    # Compute features for all symbols (last 180 days)
    python tool/compute_features.py --bar-size 1m --days 180

    # Compute for specific symbols
    python tool/compute_features.py --bar-size 1m --symbols TSLA NVDA --days 90

    # Compute for specific date range
    python tool/compute_features.py --bar-size 5m --start 2025-04-01 --end 2025-10-01

    # Force recompute (overwrite existing)
    python tool/compute_features.py --bar-size 1m --days 30 --force
"""
import argparse
import sys
from pathlib import Path
from datetime import timedelta
from typing import List, Optional

import pandas as pd
import numpy as np

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.clickhouse import ClickHouseDB
from llm_trader.ny_time import now_ny
from libs.logger import get_logger

logger = get_logger(__name__)


class FeatureComputer:
    """Compute technical features from bars data."""

    def __init__(self, bar_size: str = "1m"):
        self.bar_size = bar_size
        self.ch = ClickHouseDB()

        # Table mappings
        self.bars_table = f"bars_{bar_size}_ibkr"
        self.features_table = f"features_{bar_size}"

        logger.info("FeatureComputer initialized", bar_size=bar_size,
                   bars_table=self.bars_table, features_table=self.features_table)

    def compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Compute Average True Range."""
        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()

        return atr

    def compute_ema(self, series: pd.Series, period: int) -> pd.Series:
        """Compute Exponential Moving Average."""
        return series.ewm(span=period, adjust=False).mean()

    def compute_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        """Compute Relative Strength Index."""
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def compute_bollinger_bands(self, series: pd.Series, period: int = 20, std: float = 2.0):
        """Compute Bollinger Bands."""
        sma = series.rolling(window=period).mean()
        rolling_std = series.rolling(window=period).std()

        upper = sma + (rolling_std * std)
        lower = sma - (rolling_std * std)

        return upper, lower, sma

    def compute_volume_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute volume-based features."""
        df['vol_ema_20'] = self.compute_ema(df['volume'], 20)
        df['vol_spike'] = df['volume'] / df['vol_ema_20']
        df['z_vol_20'] = (df['volume'] - df['volume'].rolling(20).mean()) / df['volume'].rolling(20).std()

        return df

    def compute_price_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute price-based features."""
        # EMAs
        df['ema_5'] = self.compute_ema(df['close'], 5)
        df['ema_10'] = self.compute_ema(df['close'], 10)
        df['ema_20'] = self.compute_ema(df['close'], 20)
        df['ema_50'] = self.compute_ema(df['close'], 50)
        df['ema_200'] = self.compute_ema(df['close'], 200)

        # ATR
        df['atr_5'] = self.compute_atr(df, 5)
        df['atr_14'] = self.compute_atr(df, 14)

        # RSI
        df['rsi_14'] = self.compute_rsi(df['close'], 14)

        # Bollinger Bands
        df['bb_upper'], df['bb_lower'], df['bb_mid'] = self.compute_bollinger_bands(df['close'], 20, 2.0)

        # Price z-score
        df['z_price_20'] = (df['close'] - df['close'].rolling(20).mean()) / df['close'].rolling(20).std()

        # Returns
        df['returns_1'] = df['close'].pct_change(1)
        df['returns_5'] = df['close'].pct_change(5)

        return df

    def compute_regression_features(self, df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
        """Compute linear regression features."""
        def rolling_regression(series, window):
            slopes = []
            r2_values = []

            for i in range(len(series)):
                if i < window - 1:
                    slopes.append(np.nan)
                    r2_values.append(np.nan)
                    continue

                y = series.iloc[i-window+1:i+1].values
                x = np.arange(window)

                # Linear regression
                coef = np.polyfit(x, y, 1)
                slope = coef[0]

                # R-squared
                y_pred = np.polyval(coef, x)
                ss_res = np.sum((y - y_pred) ** 2)
                ss_tot = np.sum((y - np.mean(y)) ** 2)
                r2 = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0

                slopes.append(slope)
                r2_values.append(r2)

            return pd.Series(slopes, index=series.index), pd.Series(r2_values, index=series.index)

        df['slope_20'], df['r2_20'] = rolling_regression(df['close'], window)

        return df

    def compute_microstructure_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute microstructure features (spread, etc.)."""
        # If bid/ask not available, estimate spread from high-low
        if 'bid_last' in df.columns and 'ask_last' in df.columns:
            df['spread'] = df['ask_last'] - df['bid_last']
            df['spread_bps'] = (df['spread'] / df['mid_last']) * 10000
        else:
            # Estimate spread from high-low range
            df['spread'] = df['high'] - df['low']
            df['spread_bps'] = (df['spread'] / df['close']) * 10000

        return df

    def compute_all_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all technical features."""
        logger.info("Computing features", rows=len(df))

        # Sort by timestamp
        df = df.sort_values('ts').reset_index(drop=True)

        # Price features
        df = self.compute_price_features(df)

        # Volume features
        df = self.compute_volume_features(df)

        # Regression features
        df = self.compute_regression_features(df, 20)

        # Microstructure
        df = self.compute_microstructure_features(df)

        # Add feature_cutoff_ts (for point-in-time correctness)
        df['feature_cutoff_ts'] = df['ts']

        logger.info("Features computed", rows=len(df))

        return df

    def load_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Load bars from ClickHouse."""
        rth_filter = ""
        # Daily bars are already aggregated; they may not fall within intraday hours.
        if self.bar_size != "1d":
            rth_filter = """
              AND is_rth = 1
              AND toHour(ts_start) >= 9 AND toHour(ts_start) < 16
            """
        query = f"""
            SELECT
                ts_start as ts,
                symbol,
                open,
                high,
                low,
                close,
                vwap,
                volume,
                trades,
                bid_last,
                ask_last,
                mid_last
            FROM {self.bars_table}
            WHERE symbol = '{symbol}'
              AND ts_start >= '{start_date}'
              AND ts_start <= '{end_date}'
              {rth_filter}
            ORDER BY ts_start
        """

        logger.info("Loading bars", symbol=symbol, start=start_date, end=end_date, table=self.bars_table)

        try:
            result = self.ch.query(query)
            df = pd.DataFrame(result)

            if df.empty:
                logger.warning("No bars found", symbol=symbol, start=start_date, end=end_date)
                return pd.DataFrame()

            logger.info("Bars loaded", symbol=symbol, rows=len(df))
            return df

        except Exception as e:
            logger.error("Failed to load bars", symbol=symbol, error=str(e))
            return pd.DataFrame()

    def save_features(self, df: pd.DataFrame, symbol: str) -> int:
        """Save features to ClickHouse."""
        if df.empty:
            logger.warning("No features to save", symbol=symbol)
            return 0

        # Select columns to save (match existing schema)
        feature_cols = [
            'ts', 'symbol', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'trades',
            'ema_10', 'ema_20', 'ema_50',  # ema_5, ema_200 not in schema
            'atr_5', 'atr_14',
            'rsi_14',
            # Map our bb columns to schema names
            'z_price_20', 'z_vol_20',
            'vol_ema_20', 'vol_spike',
            'slope_20', 'r2_20',
            'spread_bps',
        ]

        # Rename Bollinger Band columns to match schema
        if 'bb_upper' in df.columns:
            df['bb_up_20'] = df['bb_upper']
        if 'bb_lower' in df.columns:
            df['bb_dn_20'] = df['bb_lower']
        if 'bb_mid' in df.columns:
            df['bb_mid_20'] = df['bb_mid']

        # Add missing columns that exist in schema
        feature_cols.extend(['bb_up_20', 'bb_dn_20', 'bb_mid_20'])

        # Keep only columns that exist
        available_cols = [col for col in feature_cols if col in df.columns]
        df_save = df[available_cols].copy()

        # Remove NaN rows (from warmup period)
        df_save = df_save.dropna(subset=['atr_14', 'ema_20'])

        if df_save.empty:
            logger.warning("No valid features after removing NaN", symbol=symbol)
            return 0

        # Insert in monthly batches to avoid "too many partitions" error
        df_save['month'] = pd.to_datetime(df_save['ts']).dt.to_period('M')
        total_saved = 0

        for month, group in df_save.groupby('month'):
            # Drop the month column before insert
            group_insert = group.drop(columns=['month'])

            logger.info("Saving features batch", symbol=symbol, month=str(month), rows=len(group_insert))

            try:
                self.ch.client.insert_df(self.features_table, group_insert)
                total_saved += len(group_insert)
                logger.info("Features batch saved", symbol=symbol, month=str(month), rows=len(group_insert))

            except Exception as e:
                logger.error("Failed to save features batch", symbol=symbol, month=str(month), error=str(e))

        logger.info("All features saved", symbol=symbol, total_rows=total_saved)
        return total_saved

    def process_symbol(self, symbol: str, start_date: str, end_date: str) -> int:
        """Process a single symbol - load bars, compute features, save."""
        logger.info("Processing symbol", symbol=symbol, start=start_date, end=end_date)

        # Load bars
        df = self.load_bars(symbol, start_date, end_date)
        if df.empty:
            return 0

        # Compute features
        df = self.compute_all_features(df)

        # Save features
        rows_saved = self.save_features(df, symbol)

        logger.info("Symbol processed", symbol=symbol, rows_saved=rows_saved)
        return rows_saved


def main():
    parser = argparse.ArgumentParser(description="Compute features from bars data")
    parser.add_argument("--bar-size", dest="bar_size", type=str, default="1m",
                       help="Bar size: 1m or 5m (default: 1m)")
    parser.add_argument("--symbols", nargs="+", help="Symbols to process (default: all)")
    parser.add_argument("--days", type=int, help="Number of days from today backwards")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="Force recompute (delete existing)")

    args = parser.parse_args()

    # Determine date range
    if args.days:
        end_date = now_ny().strftime("%Y-%m-%d 16:00:00")
        start_date = (now_ny() - timedelta(days=args.days)).strftime("%Y-%m-%d 09:30:00")
    elif args.start and args.end:
        start_date = f"{args.start} 09:30:00"
        end_date = f"{args.end} 16:00:00"
    else:
        # Default: last 180 days (NY day boundary)
        end_date = now_ny().strftime("%Y-%m-%d 16:00:00")
        start_date = (now_ny() - timedelta(days=180)).strftime("%Y-%m-%d 09:30:00")

    # Determine symbols
    if args.symbols:
        symbols = args.symbols
    else:
        # Default: all strategy symbols
        symbols = ["AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "TSLA"]

    logger.info("="*80)
    logger.info("Feature Computation Starting")
    logger.info("="*80)
    logger.info("Configuration", bar_size=args.bar_size, symbols=symbols,
               start=start_date, end=end_date, force=args.force)

    # Create feature computer
    computer = FeatureComputer(bar_size=args.bar_size)

    # Process each symbol
    total_rows = 0
    for symbol in symbols:
        try:
            rows = computer.process_symbol(symbol, start_date, end_date)
            total_rows += rows
        except Exception as e:
            logger.error("Failed to process symbol", symbol=symbol, error=str(e))
            continue

    logger.info("="*80)
    logger.info("Feature Computation Complete")
    logger.info("="*80)
    logger.info("Summary", total_rows=total_rows, symbols_processed=len(symbols))


if __name__ == "__main__":
    main()
