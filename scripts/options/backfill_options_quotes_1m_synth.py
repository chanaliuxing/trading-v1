#!/usr/bin/env python3
"""
Backfill synthetic option quotes (1m) for a constrained strategy universe.

Scope (default):
- underlying: QQQ
- window: 09:45-11:00 ET
- dte: 0 and 1
- strikes: ATM +/- strike_wings
- right: C/P

This is an approximate backfill for research only.
"""
from __future__ import annotations

import argparse
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.ch_pool import get_client


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price_greeks(
    s: float,
    k: float,
    t_year: float,
    r: float,
    q: float,
    sigma: float,
    right: str,
) -> tuple[float, float, float, float, float]:
    if s <= 0 or k <= 0 or t_year <= 0 or sigma <= 1e-8:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    sqrt_t = math.sqrt(t_year)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t_year) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_q = math.exp(-q * t_year)
    disc_r = math.exp(-r * t_year)

    if right == "C":
        price = disc_q * s * _norm_cdf(d1) - disc_r * k * _norm_cdf(d2)
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -disc_q * s * _norm_pdf(d1) * sigma / (2.0 * sqrt_t)
            - r * disc_r * k * _norm_cdf(d2)
            + q * disc_q * s * _norm_cdf(d1)
        ) / 365.0
    else:
        price = disc_r * k * _norm_cdf(-d2) - disc_q * s * _norm_cdf(-d1)
        delta = -disc_q * _norm_cdf(-d1)
        theta = (
            -disc_q * s * _norm_pdf(d1) * sigma / (2.0 * sqrt_t)
            + r * disc_r * k * _norm_cdf(-d2)
            - q * disc_q * s * _norm_cdf(-d1)
        ) / 365.0

    gamma = disc_q * _norm_pdf(d1) / (s * sigma * sqrt_t)
    vega = disc_q * s * _norm_pdf(d1) * sqrt_t / 100.0
    return max(price, 0.01), delta, gamma, theta, vega


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _next_weekday(d: date) -> date:
    x = d + timedelta(days=1)
    while not _is_weekday(x):
        x += timedelta(days=1)
    return x


def load_bars_1m(symbol: str, start: str, end: str) -> pd.DataFrame:
    ch = get_client()
    q = """
    SELECT
      ts_start AS ts,
      open,
      high,
      low,
      close,
      volume
    FROM trading.bars_1m_ibkr
    WHERE symbol = %(sym)s
      AND ts_start >= %(start)s
      AND ts_start <= %(end)s
      AND is_rth = 1
    ORDER BY ts_start
    """
    rows = ch.query(
        q,
        {"sym": symbol, "start": f"{start} 09:30:00", "end": f"{end} 16:00:00"},
        settings={"use_query_cache": False},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.date
    df["ret_1m"] = df["close"].pct_change().fillna(0.0)
    # Intraday realized vol proxy from recent 20m
    df["rv_20m"] = df["ret_1m"].rolling(20, min_periods=10).std().fillna(0.0) * math.sqrt(390.0)
    # Opening acceleration proxy for IV lift
    df["abs_ret_5m"] = df["ret_1m"].abs().rolling(5, min_periods=3).sum().fillna(0.0)
    return df


def synth_rows(
    bars: pd.DataFrame,
    underlying: str,
    strike_step: float,
    strike_wings: int,
    r_rate: float,
    q_div: float,
    base_iv_floor: float,
    source: str,
) -> list[dict]:
    rows: list[dict] = []
    if bars.empty:
        return rows

    for _, b in bars.iterrows():
        ts = b["ts"]
        t = ts.time()
        if t < time(9, 45) or t > time(11, 0):
            continue
        s = float(b["close"])
        if s <= 0:
            continue

        # Approx IV regime: realized vol + opening boost + slight skew smile.
        rv = float(b["rv_20m"])
        open_boost = min(0.18, float(b["abs_ret_5m"]) * 4.0)
        iv_atm = max(base_iv_floor, 0.18 + 0.65 * rv + open_boost)

        atm = round(s / strike_step) * strike_step
        strikes = [atm + i * strike_step for i in range(-strike_wings, strike_wings + 1)]
        expiries = [(b["date"], 0), (_next_weekday(b["date"]), 1)]

        for expiry, dte in expiries:
            expiry_dt = datetime.combine(expiry, time(16, 0), tzinfo=ts.tzinfo)
            t_sec = max(60.0, (expiry_dt - ts.to_pydatetime()).total_seconds())
            t_year = t_sec / (365.0 * 24.0 * 3600.0)

            for k in strikes:
                m = abs(k / s - 1.0)
                # Smile term + tiny put/call skew
                smile = 1.0 + 0.9 * (m ** 0.8)
                for right in ("C", "P"):
                    skew = 1.03 if (right == "P" and k < s) else 1.0
                    iv = max(0.08, min(2.0, iv_atm * smile * skew))
                    mid, delta, gamma, theta, vega = _bs_price_greeks(
                        s=s, k=float(k), t_year=t_year, r=r_rate, q=q_div, sigma=iv, right=right
                    )
                    # spread model widens for cheap options
                    spread_pct = min(0.35, max(0.04, 0.12 / max(mid, 0.5)))
                    spread = mid * spread_pct
                    bid = max(0.01, mid - spread / 2.0)
                    ask = max(bid + 0.01, mid + spread / 2.0)
                    spread_bps = (ask - bid) / max(mid, 0.01) * 10000.0
                    rows.append(
                        {
                            "ts": ts.to_pydatetime().replace(second=0, microsecond=0),
                            "underlying": underlying,
                            "expiry": expiry,
                            "strike": float(k),
                            "right": right,
                            "dte": int(dte),
                            "bid": float(bid),
                            "ask": float(ask),
                            "mid": float(mid),
                            "last": float(mid),
                            "volume": int(max(1, b["volume"] // 2000)),
                            "open_interest": int(max(10, b["volume"] // 200)),
                            "iv": float(iv),
                            "delta": float(delta),
                            "gamma": float(gamma),
                            "theta": float(theta),
                            "vega": float(vega),
                            "spread_bps": float(spread_bps),
                            "contracts_mult": 100,
                            "source": source,
                            "created_at": datetime.now(ts.tzinfo),
                        }
                    )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill synthetic options_quotes_1m")
    ap.add_argument("--underlying", default="QQQ")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--strike-step", type=float, default=1.0)
    ap.add_argument("--strike-wings", type=int, default=5)
    ap.add_argument("--r-rate", type=float, default=0.01)
    ap.add_argument("--q-div", type=float, default=0.0)
    ap.add_argument("--base-iv-floor", type=float, default=0.16)
    ap.add_argument("--source", default="synth_backfill_v1")
    ap.add_argument("--replace-source", action="store_true")
    args = ap.parse_args()

    bars = load_bars_1m(args.underlying.upper(), args.start, args.end)
    if bars.empty:
        print("no bars data")
        return

    rows = synth_rows(
        bars=bars,
        underlying=args.underlying.upper(),
        strike_step=float(args.strike_step),
        strike_wings=int(args.strike_wings),
        r_rate=float(args.r_rate),
        q_div=float(args.q_div),
        base_iv_floor=float(args.base_iv_floor),
        source=args.source,
    )
    print(f"synth rows={len(rows)}")
    if not rows:
        return

    ch = get_client()
    if args.replace_source:
        delete_sql = """
        ALTER TABLE trading.options_quotes_1m
        DELETE WHERE underlying = %(u)s
          AND source = %(src)s
          AND ts >= %(s)s
          AND ts <= %(e)s
        """
        ch.query(
            delete_sql,
            {
                "u": args.underlying.upper(),
                "src": args.source,
                "s": f"{args.start} 00:00:00",
                "e": f"{args.end} 23:59:59",
            },
        )

    cols = [
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
    ]
    batch = 15000
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        values = [[r[c] for c in cols] for r in chunk]
        ch.insert("trading.options_quotes_1m", values, column_names=cols)
    print("insert done")


if __name__ == "__main__":
    main()
