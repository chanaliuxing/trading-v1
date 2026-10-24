"""
tools/calibrate_ret_range_thresholds.py

Compute per-symbol dynamic thresholds for ret_h and rng_h using historical distributions.
Intended to support Phase 1 todo (P1-05/06): use quantile-based thresholds instead of hard-coded k/m.

Approach:
- Pull 1m bars from ClickHouse over a lookback window (e.g., 60–90 RTH days).
- For each symbol:
  - Compute ret_H = (P_{t+H} - P_t) / P_t
  - Compute rng_H = (max_high - min_low) / P_t over [t, t+H]
  - Take quantiles q_ret, q_rng (e.g., 0.65) to derive thresholds.
- Output a summary table (stdout) and optionally write to ClickHouse (not writing by default).

Usage:
    venv/bin/python tools/calibrate_ret_range_thresholds.py \
        --start 2025-08-01 --end 2025-10-01 \
        --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \
        --horizon-min 30 \
        --q-ret 0.65 \
        --q-rng 0.65
"""

from __future__ import annotations

import argparse
import numpy as np
import pandas as pd
from pathlib import Path
import sys
import os

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def fetch_bars(start: str, end: str, symbols: list[str]) -> pd.DataFrame:
    client = get_client()
    syms = ",".join(f"'{s}'" for s in symbols)
    sql = f"""
    SELECT
      symbol,
      ts_start AS ts,
      close,
      high,
      low
    FROM trading.bars_1m_ibkr
    WHERE symbol IN ({syms})
      AND ts_start >= toDateTime(%(start)s, 'America/New_York')
      AND ts_start <  toDateTime(%(end)s,   'America/New_York')
    ORDER BY symbol, ts
    """
    rows = client.query(sql, parameters={"start": start, "end": end})
    if not rows:
        logger.warning("No bars fetched", start=start, end=end)
        return pd.DataFrame()
    return pd.DataFrame(rows)


def compute_thresholds(df: pd.DataFrame, horizon: int, q_ret: float, q_rng: float) -> pd.DataFrame:
    results = []
    for sym, df_sym in df.groupby("symbol"):
        df_sym = df_sym.sort_values("ts").reset_index(drop=True)
        close = df_sym["close"].to_numpy()
        high = df_sym["high"].to_numpy()
        low = df_sym["low"].to_numpy()
        n = len(df_sym)
        if n <= horizon:
            continue

        ret_h = np.full(n, np.nan)
        ret_h[:-horizon] = (close[horizon:] - close[:-horizon]) / close[:-horizon]

        rng_h = np.full(n, np.nan)
        for i in range(0, n - horizon):
            window_high = high[i : i + horizon + 1]
            window_low = low[i : i + horizon + 1]
            rng_h[i] = (window_high.max() - window_low.min()) / close[i] if close[i] != 0 else np.nan

        ret_q = np.nanquantile(np.abs(ret_h), q_ret)
        rng_q = np.nanquantile(rng_h, q_rng)
        results.append(
            {
                "symbol": sym,
                "horizon_min": horizon,
                "ret_threshold": float(ret_q),
                "range_threshold": float(rng_q),
                "samples": int(np.isfinite(ret_h).sum()),
            }
        )
    return pd.DataFrame(results)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="Start datetime (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End datetime (YYYY-MM-DD)")
    parser.add_argument("--symbols", default="SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA")
    parser.add_argument(
        "--universe-file",
        default="config/option_ml_universe.json",
        help="Optional JSON file with {\"symbols\": [...]}; used if --symbols is empty",
    )
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument("--q-ret", type=float, default=0.65, help="Quantile for |ret_H|")
    parser.add_argument("--q-rng", type=float, default=0.65, help="Quantile for rng_H")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols and args.universe_file:
        try:
            with open(args.universe_file, "r", encoding="utf-8") as f:
                symbols = [s.strip().upper() for s in json.load(f).get("symbols", []) if s.strip()]
        except Exception as exc:
            logger.warning("Failed to read universe file; using empty universe", error=str(exc))
    df = fetch_bars(args.start, args.end, symbols)
    if df.empty:
        logger.error("No data fetched; aborting calibration")
        return

    res = compute_thresholds(df, args.horizon_min, args.q_ret, args.q_rng)
    if res.empty:
        logger.error("No thresholds computed; aborting calibration")
        return

    pd.set_option("display.max_rows", 100)
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()
