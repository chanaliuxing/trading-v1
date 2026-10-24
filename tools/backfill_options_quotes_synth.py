"""
tools/backfill_options_quotes_synth.py

Generate synthetic options quotes from underlying 1m bars and backfill
trading.options_quotes_1m for development/testing.

Usage:
  venv/bin/python tools/backfill_options_quotes_synth.py \
    --start 2025-12-01 --end 2025-12-31 \
    --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \
    --dte 5,7,10 \
    --replace
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import math
from pathlib import Path
import sys
from typing import Iterable, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _parse_list(s: str) -> List[str]:
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def _parse_dte_list(s: str) -> List[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bsm_price_delta_gamma_theta_vega(
    s: float,
    k: float,
    t: float,
    sigma: float,
    right: str,
) -> tuple[float, float, float, float, float]:
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t

    if right == "C":
        price = s * _norm_cdf(d1) - k * _norm_cdf(d2)
        delta = _norm_cdf(d1)
    else:
        price = k * _norm_cdf(-d2) - s * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0

    gamma = _norm_pdf(d1) / (s * vol_sqrt_t)
    vega = s * _norm_pdf(d1) * math.sqrt(t)
    theta = -s * _norm_pdf(d1) * sigma / (2.0 * math.sqrt(t))

    return price, delta, gamma, theta, vega


def _strike_grid(spot: float, pct_range: float = 0.15) -> List[float]:
    if spot < 100:
        step = 1.0
    else:
        step = 5.0
    lower = spot * (1 - pct_range)
    upper = spot * (1 + pct_range)
    start = round(lower / step) * step
    end = round(upper / step) * step
    strikes = []
    cur = start
    while cur <= end:
        strikes.append(round(cur, 2))
        cur += step
    return sorted(set(strikes))


def _fetch_bars(start: str, end: str, symbols: List[str]) -> pd.DataFrame:
    client = get_client()
    sym_list = ",".join(f"'{s}'" for s in symbols)
    sql = f"""
    SELECT toTimeZone(ts_start, 'America/New_York') AS ts, symbol, close
    FROM trading.bars_1m_ibkr
    WHERE symbol IN ({sym_list})
      AND toTimeZone(ts_start, 'America/New_York') >= toDateTime(%(start)s, 'America/New_York')
      AND toTimeZone(ts_start, 'America/New_York') <  toDateTime(%(end)s,   'America/New_York')
      AND (
        (toHour(toTimeZone(ts_start, 'America/New_York')) > 9 AND toHour(toTimeZone(ts_start, 'America/New_York')) < 16)
        OR (toHour(toTimeZone(ts_start, 'America/New_York')) = 9 AND toMinute(toTimeZone(ts_start, 'America/New_York')) >= 30)
        OR (toHour(toTimeZone(ts_start, 'America/New_York')) = 16 AND toMinute(toTimeZone(ts_start, 'America/New_York')) = 0)
      )
    ORDER BY symbol, ts
    """
    rows = client.query(sql, parameters={"start": start, "end": end})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _rolling_iv(series: pd.Series, window: int, iv_floor: float, iv_cap: float) -> pd.Series:
    log_ret = np.log(series / series.shift(1)).fillna(0.0)
    vol = log_ret.rolling(window=window, min_periods=max(5, window // 5)).std()
    annualized = vol * math.sqrt(252 * 390)
    annualized = annualized.fillna(iv_floor).clip(lower=iv_floor, upper=iv_cap)
    return annualized


def _chunked(iterable: List[tuple], size: int) -> Iterable[List[tuple]]:
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def build_synth_quotes(
    start: str,
    end: str,
    symbols: List[str],
    dte_candidates: List[int],
    iv_window_min: int,
    iv_floor: float,
    iv_cap: float,
    spread_bps: float,
    replace: bool,
    chunk_size: int,
) -> None:
    bars = _fetch_bars(start, end, symbols)
    if bars.empty:
        logger.warning("No bars fetched for synthetic options", start=start, end=end)
        return

    bars["ts"] = pd.to_datetime(bars["ts"])
    rows = []

    if replace:
        client = get_client()
        client.command(
            """
            ALTER TABLE trading.options_quotes_1m
            DELETE WHERE ts >= toDateTime(%(start)s, 'America/New_York')
              AND ts <  toDateTime(%(end)s, 'America/New_York')
            """,
            parameters={"start": start, "end": end},
        )

    for symbol, df in bars.groupby("symbol"):
        df = df.sort_values("ts").reset_index(drop=True)
        df["iv"] = _rolling_iv(df["close"], window=iv_window_min, iv_floor=iv_floor, iv_cap=iv_cap)

        for day, day_df in df.groupby(df["ts"].dt.date):
            base_spot = float(day_df.iloc[0]["close"])
            strikes = _strike_grid(base_spot)

            for _, row in day_df.iterrows():
                ts = row["ts"]
                spot = float(row["close"])
                iv = float(row["iv"])

                for dte in dte_candidates:
                    expiry = (ts.date() + timedelta(days=dte))
                    t = max(dte / 365.0, 1.0 / 365.0)
                    for strike in strikes:
                        for right in ("C", "P"):
                            price, delta, gamma, theta, vega = _bsm_price_delta_gamma_theta_vega(
                                spot, strike, t, iv, right
                            )
                            if price <= 0:
                                continue
                            spread = max(price * (spread_bps / 10000.0), 0.01)
                            bid = max(price - spread / 2.0, 0.01)
                            ask = price + spread / 2.0
                            rows.append(
                                (
                                    ts.to_pydatetime(),
                                    symbol,
                                    expiry,
                                    float(strike),
                                    right,
                                    int(dte),
                                    float(bid),
                                    float(ask),
                                    float(price),
                                    float(price),
                                    500,
                                    5000,
                                    float(iv),
                                    float(delta),
                                    float(gamma),
                                    float(theta),
                                    float(vega),
                                    float(spread_bps),
                                    100,
                                    "synthetic",
                                    datetime.now(),
                                )
                            )

    if not rows:
        logger.warning("No synthetic option quotes produced")
        return

    client = get_client()
    total = 0
    for chunk in _chunked(rows, chunk_size):
        client.insert(
            "trading.options_quotes_1m",
            chunk,
            column_names=[
                "ts",
                "underlying",
                "expiry",
                "strike",
                "right",
                "dte",
                "bid",
                "ask",
                "mid",
                "last",
                "volume",
                "open_interest",
                "iv",
                "delta",
                "gamma",
                "theta",
                "vega",
                "spread_bps",
                "contracts_mult",
                "source",
                "created_at",
            ],
        )
        total += len(chunk)
    logger.info("Inserted synthetic option quotes", rows=total)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--dte", default="5,7,10")
    parser.add_argument("--iv-window-min", type=int, default=390)
    parser.add_argument("--iv-floor", type=float, default=0.15)
    parser.add_argument("--iv-cap", type=float, default=1.0)
    parser.add_argument("--spread-bps", type=float, default=20.0)
    parser.add_argument("--chunk-size", type=int, default=50000)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    symbols = _parse_list(args.symbols)
    dte_candidates = _parse_dte_list(args.dte)

    build_synth_quotes(
        start=args.start,
        end=args.end,
        symbols=symbols,
        dte_candidates=dte_candidates,
        iv_window_min=args.iv_window_min,
        iv_floor=args.iv_floor,
        iv_cap=args.iv_cap,
        spread_bps=args.spread_bps,
        replace=args.replace,
        chunk_size=args.chunk_size,
    )


if __name__ == "__main__":
    main()
