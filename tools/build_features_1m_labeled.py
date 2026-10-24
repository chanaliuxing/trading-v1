"""
tools/build_features_1m_labeled.py

Offline label builder for intraday option ML:
- Reads 1m bars from ClickHouse (e.g., trading.bars_1m_ibkr).
- Computes forward return and range over a fixed horizon H minutes.
- Generates direction label y_dir (Up/Down/Flat) and move label y_move (Move/NoMove).
- Writes labels into trading.features_1m_labeled.

Usage:
    venv/bin/python tools/build_features_1m_labeled.py \\
        --start 2025-10-01 --end 2025-10-31 \\
        --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \\
        --horizon-min 30 --ret-threshold 0.0015 --range-threshold 0.004

Notes:
- This is a first-pass implementation optimized for clarity, not massive scale.
- For very long date ranges, process in chunks (by date or week).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from typing import List

import numpy as np
import pandas as pd

import os
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked as a script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class LabelConfig:
    symbols: List[str]
    horizon_min: int
    ret_threshold: float
    range_threshold: float
    per_symbol_thresholds: dict[str, dict[str, float]] | None = None


def _fetch_bars(start: str, end: str, cfg: LabelConfig) -> pd.DataFrame:
    """Fetch 1m bars for the given universe and date range."""
    client = get_client()
    syms = ",".join(f"'{s}'" for s in cfg.symbols)

    sql = f"""
    SELECT
      symbol,
      toTimeZone(ts_start, 'America/New_York') AS ts,
      close,
      high,
      low
    FROM trading.bars_1m_ibkr
    WHERE symbol IN ({syms})
      AND toTimeZone(ts_start, 'America/New_York') >= toDateTime(%(start)s, 'America/New_York')
      AND toTimeZone(ts_start, 'America/New_York') <  toDateTime(%(end)s,   'America/New_York')
      AND (
        (toHour(toTimeZone(ts_start, 'America/New_York')) > 9 AND toHour(toTimeZone(ts_start, 'America/New_York')) < 16)
        OR (toHour(toTimeZone(ts_start, 'America/New_York')) = 9 AND toMinute(toTimeZone(ts_start, 'America/New_York')) >= 30)
        OR (toHour(toTimeZone(ts_start, 'America/New_York')) = 16 AND toMinute(toTimeZone(ts_start, 'America/New_York')) = 0)
      )
    ORDER BY symbol, ts
    """

    logger.info("Fetching 1m bars for label generation", start=start, end=end, symbols=cfg.symbols)
    rows = client.query(sql, parameters={"start": start, "end": end})
    if not rows:
        logger.warning("No 1m bars fetched for label generation", start=start, end=end)
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    logger.info("Fetched bars", rows=len(df))
    return df


def _compute_labels_for_symbol(df_sym: pd.DataFrame, cfg: LabelConfig) -> pd.DataFrame:
    """
    Compute ret_h / rng_h / y_dir / y_move for a single symbol.
    df_sym: sorted by ts ascending.
    """
    df_sym = df_sym.sort_values("ts").reset_index(drop=True)
    close = df_sym["close"].to_numpy()
    high = df_sym["high"].to_numpy()
    low = df_sym["low"].to_numpy()

    # Horizon in bars (1m bars)
    H = cfg.horizon_min
    n = len(df_sym)

    # Forward return: (P_{t+H} - P_t) / P_t
    ret_h = np.full(n, np.nan, dtype=float)
    if H < n:
        ret_h[:-H] = (close[H:] - close[:-H]) / close[:-H]

    # Forward range: (max_high - min_low)/P_t over [t, t+H]
    rng_h = np.full(n, np.nan, dtype=float)
    if H < n:
        # For clarity, use a simple rolling loop; for large n consider numba/opt.
        for i in range(0, n - H):
            window_high = high[i : i + H + 1]
            window_low = low[i : i + H + 1]
            max_h = float(window_high.max())
            min_l = float(window_low.min())
            rng_h[i] = (max_h - min_l) / close[i] if close[i] != 0 else np.nan

    ret_threshold = cfg.ret_threshold
    range_threshold = cfg.range_threshold
    if cfg.per_symbol_thresholds:
        sym = df_sym["symbol"].iloc[0]
        sym_cfg = cfg.per_symbol_thresholds.get(sym)
        if sym_cfg:
            ret_threshold = float(sym_cfg.get("ret_threshold", ret_threshold))
            range_threshold = float(sym_cfg.get("range_threshold", range_threshold))

    # Direction label
    y_dir = np.zeros(n, dtype=int)
    up_mask = ret_h > ret_threshold
    down_mask = ret_h < -ret_threshold
    y_dir[up_mask] = 1
    y_dir[down_mask] = -1

    # Move label
    y_move = np.zeros(n, dtype=int)
    move_mask = rng_h > range_threshold
    y_move[move_mask] = 1

    labeled = pd.DataFrame(
        {
            "ts": df_sym["ts"],
            "symbol": df_sym["symbol"],
            "horizon_min": cfg.horizon_min,
            "ret_h": ret_h,
            "rng_h": rng_h,
            "y_dir": y_dir,
            "y_move": y_move,
            "ret_threshold": ret_threshold,
            "range_threshold": range_threshold,
        }
    )

    # Drop rows where ret_h/rng_h are NaN (at end of series)
    labeled = labeled.dropna(subset=["ret_h", "rng_h"])
    return labeled


def _delete_existing_labels(start: str, end: str, cfg: LabelConfig) -> None:
    client = get_client()
    symbols_list = ",".join(f"'{s}'" for s in cfg.symbols)
    sql = f"""
    ALTER TABLE trading.features_1m_labeled
    DELETE WHERE ts >= toDateTime(%(start)s, 'America/New_York')
      AND ts <  toDateTime(%(end)s, 'America/New_York')
      AND horizon_min = %(horizon)s
      AND symbol IN ({symbols_list})
    """
    logger.info("Deleting existing labels before insert", start=start, end=end, horizon=cfg.horizon_min)
    client.command(sql, parameters={"start": start, "end": end, "horizon": cfg.horizon_min})


def build_and_insert_labels(start: str, end: str, cfg: LabelConfig, *, replace: bool = False) -> None:
    df = _fetch_bars(start, end, cfg)
    if df.empty:
        return

    if replace:
        _delete_existing_labels(start, end, cfg)

    all_labeled: List[pd.DataFrame] = []
    for sym, df_sym in df.groupby("symbol"):
        logger.info("Computing labels for symbol", symbol=sym)
        labeled_sym = _compute_labels_for_symbol(df_sym, cfg)
        all_labeled.append(labeled_sym)

    if not all_labeled:
        logger.warning("No labeled rows generated")
        return

    labeled_df = pd.concat(all_labeled, ignore_index=True)
    logger.info("Total labeled rows", rows=len(labeled_df))

    client = get_client()
    records = [
        (
            row["ts"],
            row["symbol"],
            int(row["horizon_min"]),
            float(row["ret_h"]),
            float(row["rng_h"]),
            int(row["y_dir"]),
            int(row["y_move"]),
            float(row["ret_threshold"]),
            float(row["range_threshold"]),
            datetime.now(),
        )
        for _, row in labeled_df.iterrows()
    ]

    client.insert(
        "trading.features_1m_labeled",
        records,
        column_names=[
            "ts",
            "symbol",
            "horizon_min",
            "ret_h",
            "rng_h",
            "y_dir",
            "y_move",
            "ret_threshold",
            "range_threshold",
            "created_at",
        ],
    )
    logger.info("Inserted labels into trading.features_1m_labeled", rows=len(records))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="Start datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end", required=True, help="End datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    parser.add_argument(
        "--symbols",
        default="SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA",
        help="Comma-separated universe (underlyings)",
    )
    parser.add_argument(
        "--universe-file",
        default="config/option_ml_universe.json",
        help="Optional JSON file with {\"symbols\": [...]}; used if --symbols is empty",
    )
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument(
        "--horizon-mins",
        default="",
        help="Optional comma-separated horizons (e.g., 30,60). Overrides --horizon-min when provided.",
    )
    parser.add_argument("--ret-threshold", type=float, default=0.0015)
    parser.add_argument("--range-threshold", type=float, default=0.004)
    parser.add_argument(
        "--thresholds-file",
        default="config/option_ml_thresholds.json",
        help="JSON file with per-symbol thresholds; set to empty to disable",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing labels for the date range/symbols/horizon before inserting",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols and args.universe_file:
        try:
            with open(args.universe_file, "r", encoding="utf-8") as f:
                symbols = [s.strip().upper() for s in json.load(f).get("symbols", []) if s.strip()]
        except Exception as exc:
            logger.warning("Failed to read universe file; using empty universe", error=str(exc))
    per_symbol_thresholds = None
    if args.thresholds_file:
        try:
            with open(args.thresholds_file, "r", encoding="utf-8") as f:
                cfg_json = json.load(f)
            per_symbol_thresholds = cfg_json.get("symbols", {})
        except FileNotFoundError:
            logger.warning("Thresholds file not found; using global thresholds", path=args.thresholds_file)
        except Exception as exc:
            logger.warning("Failed to read thresholds file; using global thresholds", error=str(exc))

    horizons: List[int] = []
    if args.horizon_mins:
        horizons = [int(h.strip()) for h in args.horizon_mins.split(",") if h.strip()]
    if not horizons:
        horizons = [args.horizon_min]

    for horizon in horizons:
        cfg = LabelConfig(
            symbols=symbols,
            horizon_min=horizon,
            ret_threshold=args.ret_threshold,
            range_threshold=args.range_threshold,
            per_symbol_thresholds=per_symbol_thresholds,
        )
        build_and_insert_labels(args.start, args.end, cfg, replace=args.replace)


if __name__ == "__main__":
    main()
