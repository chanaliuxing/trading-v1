#!/usr/bin/env python3
"""
Check availability of required QQQ-strategy numbers from SFS (Redis).

This does NOT place any orders. It only reports whether we can compute:
1) QQQ 1m close (last completed minute)
2) QQQ VWAP (from 09:30 ET)
3) AD-NASD last value
4) TQQQ/SQQQ last trade price (from latest 1s close)
5) TQQQ/SQQQ 5m ATR(14) from aggregated 1s bars
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.dal.sfs_facade import SFS1sReader

ET = ZoneInfo("America/New_York")


def _finite_any(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def _finite_price(x: Optional[float]) -> Optional[float]:
    v = _finite_any(x)
    if v is None:
        return None
    if v <= 0:
        return None
    return v


def _ts_to_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _floor_5m(dt: datetime) -> datetime:
    m = (dt.minute // 5) * 5
    return dt.replace(minute=m, second=0, microsecond=0)


def _atr_wilder_from_5m_bars(bars: list[dict], period: int) -> Optional[float]:
    if period <= 0:
        return None
    if len(bars) < period + 1:
        return None
    bars = sorted(bars, key=lambda b: b["ts_end"])

    def tr(curr: dict, prev_close: float) -> float:
        return max(
            curr["high"] - curr["low"],
            abs(curr["high"] - prev_close),
            abs(curr["low"] - prev_close),
        )

    trs = []
    prev_close = float(bars[0]["close"])
    for b in bars[1:]:
        trs.append(tr(b, prev_close))
        prev_close = float(b["close"])
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / float(period)
    for x in trs[period:]:
        atr = (atr * (period - 1) + float(x)) / float(period)
    return float(atr)


async def _agg_last_1m_close(rows: list[dict]) -> Optional[tuple[datetime, float]]:
    if not rows:
        return None
    # rows are newest..older
    dts = []
    for r in rows:
        ts = int(r.get("ts") or 0)
        px = _finite_price(r.get("close"))
        if ts <= 0 or px is None:
            continue
        dts.append((_ts_to_dt(ts), float(px)))
    if not dts:
        return None
    dts.sort(key=lambda x: x[0])
    # Aggregate by minute, keep last seen close
    minute_to_close = {}
    for dt, px in dts:
        minute_to_close[_floor_minute(dt)] = px
    if not minute_to_close:
        return None
    last_minute = sorted(minute_to_close.keys())[-1]
    return (last_minute, float(minute_to_close[last_minute]))


async def _vwap_from_rows(rows: list[dict]) -> Optional[float]:
    if not rows:
        return None
    # rows newest..older
    pv = 0.0
    v = 0.0
    items = []
    for r in rows:
        ts = int(r.get("ts") or 0)
        px = _finite_price(r.get("close"))
        vol = float(r.get("volume") or 0.0)
        if ts <= 0 or px is None:
            continue
        dt = _ts_to_dt(ts)
        if dt.time() < time(9, 30) or dt.time() > time(16, 0):
            continue
        items.append((dt, float(px), max(0.0, vol)))
    if not items:
        return None
    items.sort(key=lambda x: x[0])
    for _, px, vol in items:
        if vol <= 0:
            continue
        pv += px * vol
        v += vol
    if v <= 0:
        return None
    return pv / v


async def _agg_5m_bars(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    items = []
    for r in rows:
        ts = int(r.get("ts") or 0)
        px = _finite_price(r.get("close"))
        vol = float(r.get("volume") or 0.0)
        if ts <= 0 or px is None:
            continue
        dt = _ts_to_dt(ts)
        items.append((dt, float(px), max(0.0, vol)))
    if not items:
        return []
    items.sort(key=lambda x: x[0])

    out = []
    cur = None
    for dt, px, vol in items:
        start = _floor_5m(dt)
        if cur is None or cur["ts_start"] != start:
            if cur is not None:
                out.append(cur)
            cur = {
                "ts_start": start,
                "ts_end": start + timedelta(minutes=5),
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": vol,
            }
        else:
            cur["high"] = max(cur["high"], px)
            cur["low"] = min(cur["low"], px)
            cur["close"] = px
            cur["volume"] += vol
    if cur is not None:
        out.append(cur)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n1s", type=int, default=6000, help="How many 1s bars to fetch per symbol")
    ap.add_argument("--atr", type=int, default=14)
    args = ap.parse_args()

    r = SFS1sReader()
    try:
        qqq = await r.last_n("QQQ", args.n1s)
        tqqq = await r.last_n("TQQQ", args.n1s)
        sqqq = await r.last_n("SQQQ", args.n1s)
        ad = await r.last("AD-NASD")

        q_close = await _agg_last_1m_close(qqq)
        vwap = await _vwap_from_rows(qqq)

        t_last = _finite_price((tqqq[0].get("close") if tqqq else None))
        s_last = _finite_price((sqqq[0].get("close") if sqqq else None))
        ad_last = _finite_any((ad.get("close") if ad else None))

        t_5m = await _agg_5m_bars(tqqq)
        s_5m = await _agg_5m_bars(sqqq)
        t_atr = _atr_wilder_from_5m_bars(t_5m, args.atr)
        s_atr = _atr_wilder_from_5m_bars(s_5m, args.atr)

        print("QQQ 1m close:", q_close)
        print("QQQ VWAP:", vwap)
        print("AD-NASD last:", ad_last)
        print("TQQQ last:", t_last)
        print("SQQQ last:", s_last)
        print(f"TQQQ ATR5m({args.atr}):", t_atr)
        print(f"SQQQ ATR5m({args.atr}):", s_atr)
    finally:
        await r.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
