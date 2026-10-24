#!/usr/bin/env python3
"""
Snapshot required QQQ-strategy data values with readiness and provenance.

Outputs:
1) QQQ 1m close
2) QQQ VWAP
3) AD-NASD current value
4) TQQQ/SQQQ last trade price
5) TQQQ/SQQQ ATR(14) on 5m bars (live/seed/fallback source)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.dal.sfs_facade import SFS1sReader

ET = ZoneInfo("America/New_York")
ADD_SYMBOL = "AD-NASD"


@dataclass
class Ohlcv5m:
    ts_start: datetime
    ts_end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _safe_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def _safe_price(x: Any) -> Optional[float]:
    v = _safe_float(x)
    if v is None or v <= 0:
        return None
    return v


def _dt_from_ms(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _floor_5m(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def _atr_wilder_5m(bars: list[Ohlcv5m], period: int) -> Optional[float]:
    if period <= 0 or len(bars) < period + 1:
        return None
    bars = sorted(bars, key=lambda b: b.ts_end)

    def tr(curr: Ohlcv5m, prev_close: float) -> float:
        return max(
            curr.high - curr.low,
            abs(curr.high - prev_close),
            abs(curr.low - prev_close),
        )

    prev_close = bars[0].close
    trs: list[float] = []
    for b in bars[1:]:
        trs.append(tr(b, prev_close))
        prev_close = b.close
    if len(trs) < period:
        return None

    atr = sum(trs[:period]) / float(period)
    for x in trs[period:]:
        atr = (atr * (period - 1) + x) / float(period)
    return float(atr)


def _agg_5m_from_rows(rows: list[dict]) -> list[Ohlcv5m]:
    items: list[tuple[datetime, float, float]] = []
    for r in rows:
        ts_ms = int(r.get("ts") or 0)
        if ts_ms <= 0:
            continue
        px = _safe_price(r.get("close"))
        if px is None:
            continue
        vol = max(0.0, float(r.get("volume") or 0.0))
        items.append((_dt_from_ms(ts_ms), px, vol))
    if not items:
        return []
    items.sort(key=lambda x: x[0])

    out: list[Ohlcv5m] = []
    cur: Optional[Ohlcv5m] = None
    for dt, px, vol in items:
        start = _floor_5m(dt)
        if cur is None or cur.ts_start != start:
            if cur is not None:
                out.append(cur)
            cur = Ohlcv5m(
                ts_start=start,
                ts_end=start + timedelta(minutes=5),
                open=px,
                high=px,
                low=px,
                close=px,
                volume=vol,
            )
            continue
        cur.high = max(cur.high, px)
        cur.low = min(cur.low, px)
        cur.close = px
        cur.volume += vol
    if cur is not None:
        out.append(cur)
    return out


def _latest_close(rows: list[dict]) -> tuple[Optional[float], Optional[datetime]]:
    for r in rows:
        ts_ms = int(r.get("ts") or 0)
        px = _safe_price(r.get("close"))
        if ts_ms > 0 and px is not None:
            return px, _dt_from_ms(ts_ms)
    return None, None


def _latest_any(rows: list[dict]) -> tuple[Optional[float], Optional[datetime]]:
    for r in rows:
        ts_ms = int(r.get("ts") or 0)
        v = _safe_float(r.get("close"))
        if ts_ms > 0 and v is not None:
            return v, _dt_from_ms(ts_ms)
    return None, None


def _last_1m_close(rows: list[dict]) -> tuple[Optional[float], Optional[datetime]]:
    minute_close: dict[datetime, float] = {}
    for r in reversed(rows):  # oldest -> newest
        ts_ms = int(r.get("ts") or 0)
        px = _safe_price(r.get("close"))
        if ts_ms <= 0 or px is None:
            continue
        minute_close[_floor_minute(_dt_from_ms(ts_ms))] = px
    if not minute_close:
        return None, None
    m = sorted(minute_close.keys())[-1]
    return float(minute_close[m]), m


def _vwap_rth(rows: list[dict]) -> Optional[float]:
    pv = 0.0
    v = 0.0
    for r in reversed(rows):  # oldest -> newest
        ts_ms = int(r.get("ts") or 0)
        px = _safe_price(r.get("close"))
        if ts_ms <= 0 or px is None:
            continue
        dt = _dt_from_ms(ts_ms)
        if dt.time() < time(9, 30) or dt.time() > time(16, 0):
            continue
        vol = max(0.0, float(r.get("volume") or 0.0))
        if vol <= 0:
            continue
        pv += px * vol
        v += vol
    if v <= 0:
        return None
    return pv / v


def _load_atr_seed(path: Path) -> dict[str, Optional[float]]:
    out = {"TQQQ": None, "SQQQ": None}
    if not path.exists():
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    if isinstance(raw, dict):
        latest = raw.get("latest")
        if isinstance(latest, dict):
            atr_pre = latest.get("atr_pre")
            if isinstance(atr_pre, dict):
                out["TQQQ"] = _safe_float(atr_pre.get("TQQQ"))
                out["SQQQ"] = _safe_float(atr_pre.get("SQQQ"))
                return out
        out["TQQQ"] = _safe_float(raw.get("TQQQ"))
        out["SQQQ"] = _safe_float(raw.get("SQQQ"))
    return out


def _fmt_ts(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt is not None else "-"


def _age_sec(now: datetime, dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n1s", type=int, default=9000, help="1s rows to pull per symbol")
    ap.add_argument("--atr", type=int, default=14, help="ATR period")
    ap.add_argument("--seed-path", default="runtime/state/qqq_intraday_atr_seed.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now = datetime.now(ET)
    seed_path = Path(args.seed_path).expanduser()
    seed = _load_atr_seed(seed_path)

    reader = SFS1sReader()
    try:
        qqq = await reader.last_n("QQQ", args.n1s)
        tqqq = await reader.last_n("TQQQ", args.n1s)
        sqqq = await reader.last_n("SQQQ", args.n1s)
        ad = await reader.last_n(ADD_SYMBOL, 5)
    finally:
        await reader.close()

    qqq_1m_close, qqq_1m_ts = _last_1m_close(qqq)
    qqq_vwap = _vwap_rth(qqq)
    ad_last, ad_ts = _latest_any(ad)
    t_last, t_ts = _latest_close(tqqq)
    s_last, s_ts = _latest_close(sqqq)

    t_bars = _agg_5m_from_rows(tqqq)
    s_bars = _agg_5m_from_rows(sqqq)
    t_atr_live = _atr_wilder_5m(t_bars, args.atr)
    s_atr_live = _atr_wilder_5m(s_bars, args.atr)

    def _effective(symbol: str, live: Optional[float]) -> tuple[Optional[float], str]:
        if live is not None and live > 0:
            return float(live), "live_5m"
        seed_v = _safe_float(seed.get(symbol))
        if seed_v is not None and seed_v > 0:
            return float(seed_v), "seed_json"
        if now.time() >= time(9, 45):
            return (0.35, "fallback") if symbol == "TQQQ" else (0.30, "fallback")
        return None, "missing"

    t_atr_eff, t_src = _effective("TQQQ", t_atr_live)
    s_atr_eff, s_src = _effective("SQQQ", s_atr_live)

    snapshot = {
        "ts_now_et": now.isoformat(),
        "symbols": {
            "QQQ": {
                "close_1m": qqq_1m_close,
                "close_1m_minute_et": _fmt_ts(qqq_1m_ts),
                "vwap_rth": qqq_vwap,
                "last_ts_age_sec": _age_sec(now, qqq_1m_ts),
            },
            "AD-NASD": {
                "value": ad_last,
                "ts_et": _fmt_ts(ad_ts),
                "age_sec": _age_sec(now, ad_ts),
            },
            "TQQQ": {
                "last": t_last,
                "ts_et": _fmt_ts(t_ts),
                "age_sec": _age_sec(now, t_ts),
                "atr5m14_live": t_atr_live,
                "atr5m14_effective": t_atr_eff,
                "atr_source": t_src,
                "bars_5m_count": len(t_bars),
            },
            "SQQQ": {
                "last": s_last,
                "ts_et": _fmt_ts(s_ts),
                "age_sec": _age_sec(now, s_ts),
                "atr5m14_live": s_atr_live,
                "atr5m14_effective": s_atr_eff,
                "atr_source": s_src,
                "bars_5m_count": len(s_bars),
            },
        },
        "seed_path": str(seed_path),
        "seed_values": seed,
    }

    ready = all(
        [
            qqq_1m_close is not None,
            qqq_vwap is not None,
            ad_last is not None,
            t_last is not None,
            s_last is not None,
            t_atr_eff is not None,
            s_atr_eff is not None,
        ]
    )
    snapshot["ready"] = ready

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return

    print(f"QQQ required snapshot @ {snapshot['ts_now_et']} (ready={ready})")
    print(f"seed_path={snapshot['seed_path']} seed={snapshot['seed_values']}")
    print(f"QQQ 1m close={qqq_1m_close} minute={_fmt_ts(qqq_1m_ts)}")
    print(f"QQQ VWAP(RTH)={qqq_vwap}")
    print(f"AD-NASD value={ad_last} ts={_fmt_ts(ad_ts)} age_sec={_age_sec(now, ad_ts)}")
    print(f"TQQQ last={t_last} ts={_fmt_ts(t_ts)} age_sec={_age_sec(now, t_ts)}")
    print(
        f"TQQQ atr5m{args.atr} live={t_atr_live} effective={t_atr_eff} "
        f"source={t_src} bars_5m={len(t_bars)}"
    )
    print(f"SQQQ last={s_last} ts={_fmt_ts(s_ts)} age_sec={_age_sec(now, s_ts)}")
    print(
        f"SQQQ atr5m{args.atr} live={s_atr_live} effective={s_atr_eff} "
        f"source={s_src} bars_5m={len(s_bars)}"
    )


if __name__ == "__main__":
    asyncio_import_err = None
    try:
        import asyncio
    except Exception as exc:  # pragma: no cover
        asyncio_import_err = exc

    if asyncio_import_err is not None:
        raise SystemExit(f"asyncio unavailable: {asyncio_import_err}")

    asyncio.run(main())
