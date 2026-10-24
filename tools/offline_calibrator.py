#!/usr/bin/env python3
"""
Offline Parameter Calibration Tool - V7.2 Strategy
Extracts the "Parameter Brain" logic from tsla_v7_2.py into a standalone utility

Usage:
    python tools/offline_calibrator.py --symbol AAPL --days 60
    python tools/offline_calibrator.py --symbol NVDA --template tsla
    python tools/offline_calibrator.py --all --days 90
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

# ---------------------- Helper Functions (from tsla_v7_2.py) ----------------------

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

def _measure_return_time_to_vwap(df1: pd.DataFrame) -> List[int]:
    """Measure typical time for price to return to VWAP after deviation"""
    vwap = _vwap_session(df1)
    spans, on, t0 = [], False, None

    # Simple Bollinger bands for deviation detection
    ma = df1['close'].rolling(20).mean()
    sd = df1['close'].rolling(20).std(ddof=0)
    bb_lo = ma - 2.0 * sd

    for i in range(21, len(df1)):
        px = float(df1['close'].iloc[i])
        vv = float(vwap.iloc[i])
        lo = float(bb_lo.iloc[i])

        if (not on) and (px < lo) and (px < vv):
            on=True
            t0=i
        elif on and px >= vv:
            spans.append(i - t0)
            on=False
            t0=None

    return [int(x) for x in spans] if spans else [40]

# ---------------------- Calibration Logic ----------------------

class ParameterCalibrator:
    """Offline parameter calibration engine"""

    def __init__(self, symbol: str, template_symbol: Optional[str] = None):
        self.symbol = symbol
        self.template_symbol = template_symbol or "TSLA"
        self.db = ClickHouseDB()

        # Load template config for defaults
        self.template_config = self._load_template_config()

    def _load_template_config(self) -> Dict[str, Any]:
        """Load template config file"""
        template_path = f"strategies/configs/{self.template_symbol.lower()}_v7_2.json"
        if os.path.exists(template_path):
            with open(template_path, 'r') as f:
                return json.load(f)

        # Fallback defaults
        return {
            "windows": {"short_days": 7, "long_days": 60, "baseline_days": 180},
            "target_triggers": 4,
            "regime": {"epsilon": 0.0048},
            "pause": {"theta_vix": 0.12, "theta_spread": 0.0007, "theta_atr": 2.1, "t_pause_min": 15},
            "A1": {"rvol_lo": 0.85, "rvol_hi": 1.80},
            "A2": {"oneway_thr": 2.2, "max_layers": 4, "grid_g_floor": 0.8},
            "B1": {"orb_len": 30, "rvol_min": 0.85, "pullback_window_M": 6, "extend_orb": False},
            "premarket_calibration": {
                "bounds": {
                    "A1_thr_sigma": {"min": 0.004, "max": 0.020},
                    "A2_grid_g": {"min": 0.6, "max": 3.5},
                    "k_atr": {"min": 1.4, "max": 3.0},
                    "A1_rvol_lo": {"min": 0.75, "max": 1.10},
                    "A1_rvol_hi": {"min": 1.0, "max": 2.5},
                    "A1_time_exit_min": {"min": 25, "max": 60}
                }
            },
            "anti_starvation": {
                "relax_sigma": [0.9, 0.8],
                "relax_g": [0.9, 0.8]
            }
        }

    async def _fetch_historical_data(self, symbol: str, resolution: str, days: int) -> Optional[pd.DataFrame]:
        """Fetch historical data from ClickHouse"""
        logger.info(f"Fetching {days} days of {resolution} data for {symbol}...")

        # Map resolution to table
        table_map = {
            "1m": "bars_1m_ibkr",
            "5m": "bars_5m_ibkr"
        }

        table = table_map.get(resolution)
        if not table:
            logger.error(f"Unsupported resolution: {resolution}")
            return None

        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        sql = f"""
        SELECT
            ts_start as timestamp,
            open, high, low, close, volume
        FROM trading.{table}
        WHERE symbol = %(symbol)s
          AND d >= %(start_date)s
          AND d <= %(end_date)s
        ORDER BY ts_start
        """

        try:
            rows = self.db.query(sql, {"symbol": symbol, "start_date": start_date, "end_date": end_date})
            if not rows:
                logger.warning(f"No data found for {symbol} in {table}")
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

            logger.info(f"Fetched {len(df)} bars for {symbol} ({resolution})")
            return df

        except Exception as e:
            logger.error(f"Failed to fetch data: {e}")
            return None

    async def calibrate(self, days: int = 60) -> Dict[str, Any]:
        """
        Main calibration logic - extracted from _premarket_calibrate()

        Returns calibrated parameters dictionary
        """
        logger.info(f"Calibrating parameters for {self.symbol} using {days} days of data...")

        # Fetch historical data
        df1 = await self._fetch_historical_data(self.symbol, "1m", min(days, 35))  # ~2730 bars for 7 days
        df5 = await self._fetch_historical_data(self.symbol, "5m", days)

        if df1 is None or df1.empty or df5 is None or df5.empty:
            logger.error("Insufficient historical data for calibration")
            return {}

        # Clean to RTH hours only
        df1 = _clean_rth(df1)
        df5 = _clean_rth(df5)

        if df1.empty or df5.empty:
            logger.error("No RTH data available after filtering")
            return {}

        logger.info(f"Data loaded: {len(df1)} 1m bars, {len(df5)} 5m bars")

        # Get bounds from template
        precal_cfg = self.template_config.get("premarket_calibration", {})
        bounds = precal_cfg.get("bounds", {})

        # A1: thr_sigma (P60 of deviation distribution)
        vwap1 = _vwap_session(df1)
        dev_series = (df1['close'] - vwap1) / vwap1
        thr_sigma = float(np.clip(_percentile(dev_series.abs(), 60, 0.007), 0.004, 0.020))
        logger.info(f"A1 thr_sigma: {thr_sigma:.6f}")

        # A1: RVOL windows (P30-P80 of 7-day RVOL)
        rvol = _intraday_rvol_5m(df5.tail(78*min(7, len(df5)//78)), lookback_days=min(7,5))
        A1_rvol_lo = float(np.clip(_percentile(rvol, 30, 0.85), 0.75, 1.00))
        A1_rvol_hi = float(np.clip(_percentile(rvol, 80, 1.8), 1.3, 2.5))
        logger.info(f"A1 RVOL range: {A1_rvol_lo:.2f} - {A1_rvol_hi:.2f}")

        # A2: grid_g (P28 of 5m true range)
        a2_bounds = bounds.get("A2_grid_g", {"min": 0.6, "max": 3.5})
        h, l, c = df5['high'], df5['low'], df5['close']
        tr5 = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        A2_grid_g = float(np.clip(_percentile(tr5, 28, 1.0), a2_bounds["min"], a2_bounds["max"]))
        A2_grid_g_floor = float(np.clip(_percentile(tr5, 20, 0.8), 0.4, 3.0))
        logger.info(f"A2 grid_g: {A2_grid_g:.4f}, floor: {A2_grid_g_floor:.4f}")

        # A2: oneway_thr and max_layers (from template)
        A2_oneway_thr = self.template_config.get("A2", {}).get("oneway_thr", 2.2)
        A2_max_layers = self.template_config.get("A2", {}).get("max_layers", 4)

        # B1: ORB length and RVOL min
        B1_orb_len = self.template_config.get("B1", {}).get("orb_len", 30)
        B1_rvol_min = float(np.clip(_percentile(rvol, 40, 0.85), 0.75, 1.10))
        B1_pullback_M = self.template_config.get("B1", {}).get("pullback_window_M", 6)
        logger.info(f"B1 ORB len: {B1_orb_len}, rvol_min: {B1_rvol_min:.2f}")

        # k_ATR: P90 of |ΔC_5m| / mean(ATR_5m)
        k_atr_bounds = bounds.get("k_atr", {"min": 1.4, "max": 3.0})
        atr5 = _atr_wilder(df5, 14)
        atr_mean = float(np.nanmean(atr5.tail(78))) if len(atr5)>=78 else float(np.nanmean(atr5))
        ret_abs = df5['close'].diff().abs().dropna()
        ratio = ( _percentile(ret_abs, 90, 1.2) / max(atr_mean, 1e-6) )
        k_atr = float(np.clip(1.2 + 0.4*((ratio-1.2)/(3.0-1.2)), k_atr_bounds["min"], k_atr_bounds["max"]))
        logger.info(f"k_ATR: {k_atr:.4f}")

        # A1: time_exit_min (median return-to-VWAP time)
        return_times = _measure_return_time_to_vwap(df1)
        A1_time_exit_min = int(np.clip(np.nanmedian(return_times), 25, 60))
        logger.info(f"A1 time_exit: {A1_time_exit_min} minutes")

        # regime_epsilon (from template)
        regime_epsilon = self.template_config.get("regime", {}).get("epsilon", 0.0048)

        # Compile calibrated parameters
        calibrated = {
            "windows_short": self.template_config.get("windows", {}).get("short_days", 7),
            "k_atr": k_atr,
            "regime_epsilon": regime_epsilon,
            "pause": self.template_config.get("pause", {}),
            "A1_thr_sigma": thr_sigma,
            "A1_rvol_lo": A1_rvol_lo,
            "A1_rvol_hi": A1_rvol_hi,
            "A1_time_exit_min": A1_time_exit_min,
            "A2_grid_g": A2_grid_g,
            "A2_grid_g_floor": A2_grid_g_floor,
            "A2_oneway_thr": A2_oneway_thr,
            "A2_max_layers": A2_max_layers,
            "B1_orb_len": B1_orb_len,
            "B1_rvol_min": B1_rvol_min,
            "B1_pullback_window_M": B1_pullback_M,
            "B1_extend_orb": self.template_config.get("B1", {}).get("extend_orb", False)
        }

        # Trigger rate check (rough estimate)
        est_triggers = self._estimate_triggers(df1, df5, calibrated)
        target_triggers = self.template_config.get("target_triggers", 4)

        logger.info(f"Estimated triggers per day: {est_triggers:.1f} (target: {target_triggers})")

        if est_triggers < 0.5 * target_triggers:
            logger.warning("Low trigger rate detected, applying anti-starvation relaxation")
            relax = self.template_config.get("anti_starvation", {})
            calibrated["A1_thr_sigma"] *= relax.get("relax_sigma", [0.9, 0.8])[0]
            calibrated["A2_grid_g"] *= relax.get("relax_g", [0.9, 0.8])[0]
            calibrated["A2_grid_g"] = max(calibrated["A2_grid_g"], calibrated["A2_grid_g_floor"])

        return calibrated

    def _estimate_triggers(self, df1: pd.DataFrame, df5: pd.DataFrame, params: Dict[str, Any]) -> float:
        """Rough estimate of daily trigger rate"""
        # Count A1 crossings
        vwap = _vwap_session(df1)
        dev = (df1['close'] - vwap) / vwap
        a1_cnt = int((dev < -float(params["A1_thr_sigma"])).sum())

        # Count A2 grid touches
        sma30 = _ema(df5['close'], span=30)
        g = float(params["A2_grid_g"])
        a2_cnt = int((df5['close'] <= (sma30 - g)).sum())

        # Weighted average (A1 more likely to trigger)
        total_days = max(len(df1) / (78 * 5), 1)  # Approx trading days
        daily_triggers = (0.1 * a1_cnt + 0.05 * a2_cnt) / total_days

        return daily_triggers

    def save_results(self, params: Dict[str, Any], output_dir: str = "."):
        """Save calibrated parameters to LKG file"""
        output_file = f"{output_dir}/{self.symbol.lower()}_v7_2_lkg.json"

        with open(output_file, 'w') as f:
            json.dump(params, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved calibrated parameters to {output_file}")
        return output_file

# ---------------------- CLI Interface ----------------------

async def main():
    parser = argparse.ArgumentParser(description="Offline Parameter Calibration Tool for V7.2 Strategy")
    parser.add_argument("--symbol", type=str, help="Symbol to calibrate (e.g., AAPL)")
    parser.add_argument("--all", action="store_true", help="Calibrate all symbols in universe")
    parser.add_argument("--days", type=int, default=60, help="Days of historical data to use (default: 60)")
    parser.add_argument("--template", type=str, default="TSLA", help="Template config to use (default: TSLA)")
    parser.add_argument("--output-dir", type=str, default=".", help="Output directory for LKG files")

    args = parser.parse_args()

    if args.all:
        # Calibrate all symbols in universe
        universe = getattr(config, 'universe', [])
        if not universe:
            logger.error("No universe list found in config")
            return

        logger.info(f"Calibrating {len(universe)} symbols from universe: {universe}")

        for symbol in universe:
            # Skip reference-only symbols
            if symbol in ["SPY", "VIX", "SQQQ"]:
                logger.info(f"Skipping {symbol} (reference only)")
                continue

            logger.info(f"\n{'='*80}\nCalibrating {symbol}\n{'='*80}")

            calibrator = ParameterCalibrator(symbol, template_symbol=args.template)
            params = await calibrator.calibrate(days=args.days)

            if params:
                calibrator.save_results(params, output_dir=args.output_dir)
            else:
                logger.error(f"Failed to calibrate {symbol}")

    elif args.symbol:
        # Calibrate single symbol
        symbol = args.symbol.upper()
        logger.info(f"Calibrating {symbol} using {args.days} days of data")

        calibrator = ParameterCalibrator(symbol, template_symbol=args.template)
        params = await calibrator.calibrate(days=args.days)

        if params:
            calibrator.save_results(params, output_dir=args.output_dir)

            # Print summary
            print(f"\n{'='*80}")
            print(f"Calibration Results for {symbol}")
            print(f"{'='*80}")
            print(f"A1 thr_sigma:     {params['A1_thr_sigma']:.6f}")
            print(f"A1 RVOL range:    {params['A1_rvol_lo']:.2f} - {params['A1_rvol_hi']:.2f}")
            print(f"A1 time_exit:     {params['A1_time_exit_min']} minutes")
            print(f"A2 grid_g:        {params['A2_grid_g']:.4f}")
            print(f"A2 grid_g_floor:  {params['A2_grid_g_floor']:.4f}")
            print(f"k_ATR:            {params['k_atr']:.4f}")
            print(f"B1 ORB length:    {params['B1_orb_len']} minutes")
            print(f"B1 RVOL min:      {params['B1_rvol_min']:.2f}")
            print(f"{'='*80}\n")
        else:
            logger.error(f"Calibration failed for {symbol}")

    else:
        parser.print_help()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCalibration cancelled by user")
    except Exception as e:
        logger.error(f"Calibration error: {e}", exc_info=True)
        sys.exit(1)
