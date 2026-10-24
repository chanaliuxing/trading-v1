#!/usr/bin/env python3
"""
Intraday Parameter Suggestion Tool - V7.2 Strategy
Generates lightweight parameter adjustments during live trading hours

Usage:
    python tools/intraday_calibrator.py --symbol TSLA
    python tools/intraday_calibrator.py --all
    python tools/intraday_calibrator.py --symbol AAPL --window 120
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

# ---------------------- Helper Functions ----------------------

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()

def _atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def _percentile(x: pd.Series, p: float, default=np.nan) -> float:
    x = x.dropna()
    if len(x) == 0: return float(default)
    return float(np.nanpercentile(x.values, p))

def _clean_rth(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to RTH hours and clean outliers"""
    df = df[['open','high','low','close','volume']].dropna()
    df = df[~(df['close'].pct_change().abs() > 0.25)]
    return df.between_time("09:30","16:00")

def _vwap_session(df: pd.DataFrame) -> pd.Series:
    """Calculate intraday VWAP"""
    q = df['volume'].clip(lower=0.0)
    pq = df['close'] * q
    return (pq.cumsum() / q.cumsum()).replace([np.inf, -np.inf], np.nan).ffill()

def _slot_index(ts: pd.Timestamp) -> int:
    """Calculate 5-minute slot index from 09:30"""
    return int(((ts.hour - 9) * 60 + ts.minute - 30) // 5)

def _intraday_rvol_5m(df_5m: pd.DataFrame, lookback_days: int = 5) -> pd.Series:
    """Calculate relative volume by time-of-day"""
    d = df_5m.copy()
    d['slot'] = d.index.map(_slot_index)
    vol2 = d['volume'].rolling(2).sum()
    out = []
    for i, (ts, row) in enumerate(d.iterrows()):
        if i < 78*lookback_days:
            out.append(np.nan)
            continue
        same = d.iloc[i-78*lookback_days:i]
        same = same[same['slot'] == row['slot']]
        denom = same['volume'].rolling(2).sum().median()
        out.append(float((vol2.iloc[i]/denom) if denom and denom>0 else 1.0))
    return pd.Series(out, index=d.index)

# ---------------------- Intraday Suggestion Engine ----------------------

class IntradaySuggester:
    """
    Lightweight intraday parameter adjustment engine

    Purpose:
    - Monitors realized volatility and signal quality
    - Suggests micro-adjustments to k_atr, A1_thr_sigma, A2_grid_g
    - Does NOT replace premarket calibration
    - Provides suggestions for smooth EMA blending
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.db = ClickHouseDB()
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load current strategy config"""
        config_path = f"strategies/configs/{self.symbol.lower()}_v7_2.json"
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.load(f)
        return {}

    async def _fetch_today_data(self, resolution: str, window_minutes: int = 120) -> Optional[pd.DataFrame]:
        """Fetch today's data from ClickHouse"""
        table_map = {
            "1m": "bars_1m_current",
            "5m": "bars_5m_current"
        }

        table = table_map.get(resolution)
        if not table:
            logger.error(f"Unsupported resolution: {resolution}")
            return None

        now = datetime.now()
        start_time = now - timedelta(minutes=window_minutes)

        sql = f"""
        SELECT
            ts_end as timestamp,
            open, high, low, close, volume
        FROM trading.{table}
        WHERE symbol = %(symbol)s
          AND ts_end >= %(start_time)s
        ORDER BY ts_end
        """

        try:
            rows = self.db.query(sql, {
                "symbol": self.symbol,
                "start_time": start_time
            })

            if not rows:
                logger.warning(f"No data found for {self.symbol}")
                return None

            df = pd.DataFrame(rows)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.set_index('timestamp').sort_index()

            # Localize to NY timezone
            try:
                df.index = df.index.tz_localize("America/New_York")
            except:
                try:
                    df.index = df.index.tz_convert("America/New_York")
                except:
                    pass

            return df

        except Exception as e:
            logger.error(f"Failed to fetch today's data: {e}")
            return None

    async def suggest_adjustments(self, window_minutes: int = 120) -> Dict[str, Any]:
        """
        Generate intraday parameter suggestions based on recent market behavior

        Args:
            window_minutes: Lookback window for analysis (default: 2 hours)

        Returns:
            Dictionary with suggested parameter adjustments
        """
        logger.info(f"Generating intraday suggestions for {self.symbol} (window: {window_minutes}m)")

        # Fetch recent data
        df1 = await self._fetch_today_data("1m", window_minutes)
        df5 = await self._fetch_today_data("5m", window_minutes)

        if df1 is None or df1.empty or df5 is None or df5.empty:
            logger.warning("Insufficient data for intraday suggestions")
            return {}

        # Clean to RTH only
        df1 = _clean_rth(df1)
        df5 = _clean_rth(df5)

        if df1.empty or df5.empty:
            logger.warning("No RTH data available")
            return {}

        logger.info(f"Analyzing {len(df1)} 1m bars, {len(df5)} 5m bars")

        suggestions = {}

        # 1. k_atr adjustment based on realized volatility
        if len(df5) >= 20:
            atr5 = _atr_wilder(df5, 14).iloc[-1]
            recent_range = df5['close'].iloc[-12:].std()  # Last hour volatility

            # If recent volatility > ATR, suggest increase
            vol_ratio = recent_range / (atr5 + 1e-9)

            current_k_atr = self.config.get("k_atr", 2.0)
            bounds = self.config.get("premarket_calibration", {}).get("bounds", {}).get("k_atr", {"min": 1.4, "max": 3.0})

            if vol_ratio > 1.2:
                # High volatility: increase k_atr
                suggested_k_atr = min(current_k_atr * 1.1, bounds["max"])
                suggestions["k_atr"] = suggested_k_atr
                logger.info(f"k_atr suggestion: {suggested_k_atr:.4f} (current: {current_k_atr:.4f}, reason: high_vol)")
            elif vol_ratio < 0.8:
                # Low volatility: decrease k_atr
                suggested_k_atr = max(current_k_atr * 0.95, bounds["min"])
                suggestions["k_atr"] = suggested_k_atr
                logger.info(f"k_atr suggestion: {suggested_k_atr:.4f} (current: {current_k_atr:.4f}, reason: low_vol)")

        # 2. A1_thr_sigma adjustment based on VWAP deviation distribution
        if len(df1) >= 60:
            vwap = _vwap_session(df1)
            dev = (df1['close'] - vwap) / vwap
            recent_dev_p60 = _percentile(dev.abs().tail(60), 60, 0.007)

            current_thr = self.config.get("A1", {}).get("thr_sigma", 0.009)
            bounds = self.config.get("premarket_calibration", {}).get("bounds", {}).get("A1_thr_sigma", {"min": 0.004, "max": 0.020})

            # Suggest threshold based on recent deviation
            suggested_thr = np.clip(recent_dev_p60 * 0.9, bounds["min"], bounds["max"])

            if abs(suggested_thr - current_thr) / current_thr > 0.15:  # 15% difference threshold
                suggestions["A1_thr_sigma"] = suggested_thr
                logger.info(f"A1_thr_sigma suggestion: {suggested_thr:.6f} (current: {current_thr:.6f})")

        # 3. A2_grid_g adjustment based on recent true range
        if len(df5) >= 20:
            h, l, c = df5['high'], df5['low'], df5['close']
            tr5 = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
            recent_tr_p28 = _percentile(tr5.tail(24), 28, 1.0)  # Last 2 hours

            current_g = self.config.get("A2", {}).get("grid_g", 1.2)
            bounds = self.config.get("premarket_calibration", {}).get("bounds", {}).get("A2_grid_g", {"min": 0.6, "max": 3.5})

            suggested_g = np.clip(recent_tr_p28, bounds["min"], bounds["max"])

            if abs(suggested_g - current_g) / current_g > 0.20:  # 20% difference threshold
                suggestions["A2_grid_g"] = suggested_g
                logger.info(f"A2_grid_g suggestion: {suggested_g:.4f} (current: {current_g:.4f})")

        # 4. RVOL window adjustments
        if len(df5) >= 78:
            try:
                rvol = _intraday_rvol_5m(df5.tail(78), lookback_days=5)
                recent_rvol = rvol.tail(12)  # Last hour

                if len(recent_rvol.dropna()) > 0:
                    current_lo = self.config.get("A1", {}).get("rvol_lo", 0.85)
                    current_hi = self.config.get("A1", {}).get("rvol_hi", 1.80)

                    suggested_lo = float(np.clip(_percentile(recent_rvol, 30, current_lo), 0.75, 1.00))
                    suggested_hi = float(np.clip(_percentile(recent_rvol, 80, current_hi), 1.3, 2.5))

                    if abs(suggested_lo - current_lo) / current_lo > 0.10:
                        suggestions["A1_rvol_lo"] = suggested_lo
                        logger.info(f"A1_rvol_lo suggestion: {suggested_lo:.2f} (current: {current_lo:.2f})")

                    if abs(suggested_hi - current_hi) / current_hi > 0.10:
                        suggestions["A1_rvol_hi"] = suggested_hi
                        logger.info(f"A1_rvol_hi suggestion: {suggested_hi:.2f} (current: {current_hi:.2f})")
            except Exception as e:
                logger.warning(f"RVOL calculation failed: {e}")

        return suggestions

    def save_suggestions(self, suggestions: Dict[str, Any], output_dir: str = "params"):
        """Save intraday suggestions to file"""
        os.makedirs(output_dir, exist_ok=True)
        output_file = f"{output_dir}/{self.symbol.lower()}_v7_2_intraday.json"

        # Add metadata
        suggestions_with_meta = {
            **suggestions,
            "_generated_at": datetime.now().isoformat(),
            "_symbol": self.symbol
        }

        with open(output_file, 'w') as f:
            json.dump(suggestions_with_meta, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved intraday suggestions to {output_file}")
        return output_file

# ---------------------- CLI Interface ----------------------

async def main():
    parser = argparse.ArgumentParser(description="Intraday Parameter Suggestion Tool for V7.2 Strategy")
    parser.add_argument("--symbol", type=str, help="Symbol to analyze (e.g., TSLA)")
    parser.add_argument("--all", action="store_true", help="Generate suggestions for all symbols")
    parser.add_argument("--window", type=int, default=120, help="Lookback window in minutes (default: 120)")
    parser.add_argument("--output-dir", type=str, default="params", help="Output directory for suggestion files")

    args = parser.parse_args()

    if args.all:
        # Get universe from config
        universe = getattr(config, 'universe', [])
        if not universe:
            logger.error("No universe list found in config")
            return

        # Filter out reference-only symbols
        symbols = [s for s in universe if s not in ["SPY", "VIX", "SQQQ"]]
        logger.info(f"Generating suggestions for {len(symbols)} symbols: {symbols}")

        for symbol in symbols:
            logger.info(f"\n{'='*80}\nAnalyzing {symbol}\n{'='*80}")

            suggester = IntradaySuggester(symbol)
            suggestions = await suggester.suggest_adjustments(window_minutes=args.window)

            if suggestions:
                suggester.save_suggestions(suggestions, output_dir=args.output_dir)
                print(f"\n{symbol}: {len(suggestions)} suggestions generated")
            else:
                logger.warning(f"{symbol}: No suggestions (market conditions stable or insufficient data)")

    elif args.symbol:
        symbol = args.symbol.upper()
        logger.info(f"Analyzing {symbol} (window: {args.window} minutes)")

        suggester = IntradaySuggester(symbol)
        suggestions = await suggester.suggest_adjustments(window_minutes=args.window)

        if suggestions:
            suggester.save_suggestions(suggestions, output_dir=args.output_dir)

            # Print summary
            print(f"\n{'='*80}")
            print(f"Intraday Suggestions for {symbol}")
            print(f"{'='*80}")
            for key, value in suggestions.items():
                if not key.startswith("_"):
                    print(f"{key:20s}: {value}")
            print(f"{'='*80}\n")
        else:
            logger.info(f"No adjustments needed for {symbol} (market conditions stable)")

    else:
        parser.print_help()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAnalysis cancelled by user")
    except Exception as e:
        logger.error(f"Analysis error: {e}", exc_info=True)
        sys.exit(1)
