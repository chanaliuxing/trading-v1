#!/usr/bin/env python3
"""
Capture option chain snapshots into trading.options_quotes_1m.

Usage:
  python3 scripts/options/capture_options_quotes_1m.py --underlying QQQ --once
  python3 scripts/options/capture_options_quotes_1m.py --underlying QQQ --interval-sec 60
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.ch_pool import get_client
from services.market.options_data import create_options_provider

ET = ZoneInfo("America/New_York")


def _ts_floor_min(dt: datetime) -> datetime:
    return dt.astimezone(ET).replace(second=0, microsecond=0)


def _safe_dte(expiry_date: str, now: datetime) -> int:
    try:
        e = datetime.fromisoformat(str(expiry_date)).date()
        return int((e - now.date()).days)
    except Exception:
        return 999


def _safe_int(x: object, default: int = 0) -> int:
    try:
        if x is None:
            return default
        v = float(x)
        if v != v:  # NaN
            return default
        return int(v)
    except Exception:
        return default


async def capture_once(
    underlying: str,
    dtes: list[int],
    max_strike_dist_pct: float,
    trading_mode: str,
) -> int:
    provider = create_options_provider(
        "ibkr",
        host="127.0.0.1",
        port=(4001 if trading_mode == "live" else 4002),
    )
    ch = get_client()
    now = _ts_floor_min(datetime.now(ET))

    try:
        spot = await provider.get_underlying_price(underlying)
        if not spot or spot <= 0:
            return 0

        rows_to_insert = []
        for right in ("C", "P"):
            chain = await provider.get_chain(underlying, dtes, right=right)
            if not chain:
                continue
            for c in chain:
                if c.strike <= 0:
                    continue
                if abs((c.strike - spot) / spot) > max_strike_dist_pct:
                    continue
                mid = 0.0
                if c.bid is not None and c.ask is not None and c.bid > 0 and c.ask > 0:
                    mid = (float(c.bid) + float(c.ask)) / 2.0
                rows_to_insert.append(
                    {
                        "ts": now,
                        "underlying": underlying,
                        "expiry": c.expiry_date,
                        "strike": float(c.strike),
                        "right": right,
                        "dte": _safe_dte(c.expiry_date, now),
                        "bid": float(c.bid or 0.0),
                        "ask": float(c.ask or 0.0),
                        "mid": float(mid),
                        "last": float(c.last or 0.0),
                        "volume": _safe_int(c.volume, 0),
                        "open_interest": _safe_int(c.open_interest, 0),
                        "iv": float(c.implied_vol or 0.0),
                        "delta": float(c.delta or 0.0),
                        "gamma": float(c.gamma or 0.0),
                        "theta": float(c.theta or 0.0),
                        "vega": float(c.vega or 0.0),
                        "spread_bps": 0.0 if mid <= 0 else (float(c.ask or 0.0) - float(c.bid or 0.0)) / mid * 10000.0,
                        "contracts_mult": 100,
                        "source": "ibkr",
                        "created_at": datetime.now(ET),
                    }
                )

        if not rows_to_insert:
            return 0

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
        ch.insert("trading.options_quotes_1m", rows_to_insert, column_names=cols)
        return len(rows_to_insert)
    finally:
        if hasattr(provider, "disconnect"):
            try:
                await provider.disconnect()
            except Exception:
                pass


async def main() -> None:
    ap = argparse.ArgumentParser(description="Capture option quotes to ClickHouse")
    ap.add_argument("--underlying", default="QQQ")
    ap.add_argument("--dtes", default="0,1")
    ap.add_argument("--max-strike-dist-pct", type=float, default=0.06)
    ap.add_argument("--interval-sec", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = ap.parse_args()

    dtes = [int(x.strip()) for x in args.dtes.split(",") if x.strip()]

    while True:
        try:
            n = await capture_once(
                underlying=args.underlying.upper(),
                dtes=dtes,
                max_strike_dist_pct=float(args.max_strike_dist_pct),
                trading_mode=args.mode,
            )
            print(f"{datetime.now(ET).isoformat()} inserted={n}")
        except Exception as exc:
            print(f"{datetime.now(ET).isoformat()} capture_failed error={exc}")
        if args.once:
            break
        await asyncio.sleep(max(10, int(args.interval_sec)))


if __name__ == "__main__":
    asyncio.run(main())
