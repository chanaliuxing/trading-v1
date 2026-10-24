from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.dal.sfs_facade import SFS1sReader
from libs.db.clickhouse import ClickHouseDB

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Bar:
    ts: datetime  # minute start in ET
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    day: str
    symbol: str
    side: str  # LONG/SHORT (maps to TQQQ/SQQQ)
    qty: int
    entry_ts: datetime
    entry_px: float
    stop_px: float
    target: float
    exit_ts: Optional[datetime] = None
    exit_px: Optional[float] = None
    exit_reason: Optional[str] = None
    mfe_px: float = 0.0
    mae_px: float = 0.0
    entry_track: str = ""  # FAST/CONFIRM (for alternative entry models)

    def pnl_usd(self) -> Optional[float]:
        if self.exit_px is None:
            return None
        return float(self.qty) * (float(self.exit_px) - float(self.entry_px))


def _minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _parse_day(s: str) -> date:
    return datetime.fromisoformat(s).date()


def _ts_et(d: str, hhmm: str) -> datetime:
    hh, mm = [int(x) for x in hhmm.split(":")]
    return datetime.fromisoformat(d).replace(hour=hh, minute=mm, second=0, microsecond=0, tzinfo=ET)


def _sorted_minutes(bars: dict[str, dict[datetime, Bar]]) -> list[datetime]:
    base = bars.get("QQQ") or {}
    return sorted(base.keys())


def _vwap_up_to(bars_by_minute: dict[datetime, Bar], up_to_ts: datetime) -> float:
    num = 0.0
    den = 0.0
    for ts, b in bars_by_minute.items():
        if ts > up_to_ts:
            continue
        px = (b.high + b.low + b.close) / 3.0
        vol = max(0.0, float(b.volume))
        num += px * vol
        den += vol
    return float(num / den) if den > 0 else 0.0


def _atr(bars_by_minute: dict[datetime, Bar], up_to_ts: datetime, period: int) -> Optional[float]:
    ts_list = [ts for ts in sorted(bars_by_minute.keys()) if ts <= up_to_ts]
    if len(ts_list) < period + 1:
        return None
    trs: list[float] = []
    for i in range(-period, 0):
        cur = bars_by_minute[ts_list[i]]
        prev = bars_by_minute[ts_list[i - 1]]
        tr = max(
            float(cur.high - cur.low),
            abs(float(cur.high - prev.close)),
            abs(float(cur.low - prev.close)),
        )
        trs.append(float(tr))
    return float(sum(trs) / len(trs)) if trs else None


def _close_at_or_before(bars_by_minute: dict[datetime, Bar], ts: datetime) -> Optional[float]:
    keys = [k for k in bars_by_minute.keys() if k <= ts]
    if not keys:
        return None
    k = max(keys)
    return float(bars_by_minute[k].close)


def _load_atr_seed(path: Path, day: str) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, float] = {}

    # Current on-disk shape (runtime/state/qqq_intraday_atr_seed.json):
    # {"days": {"YYYY-MM-DD": {"TQQQ": <float>, "SQQQ": <float>}, ...}, "latest": {...}}
    rec = (raw.get("days") or {}).get(day) or raw.get(day) or {}
    if isinstance(rec, dict):
        for sym in ("TQQQ", "SQQQ"):
            v = rec.get(sym)
            if isinstance(v, (int, float)):
                out[sym] = float(v)
            elif isinstance(v, dict) and "atr_pre" in v:
                try:
                    out[sym] = float(v["atr_pre"])
                except Exception:
                    pass

    if (not out) and isinstance(raw.get("latest"), dict):
        latest = raw.get("latest") or {}
        if str(latest.get("day")) == str(day):
            atr_pre = latest.get("atr_pre") or {}
            for sym in ("TQQQ", "SQQQ"):
                if sym in atr_pre:
                    out[sym] = float(atr_pre[sym])

    return out


def _synth_from_qqq(qqq: dict[datetime, Bar], base_tqqq: float = 50.0, base_sqqq: float = 70.0) -> dict[str, dict[datetime, Bar]]:
    """
    Create rough synthetic QQEW/TQQQ/SQQQ minute bars from QQQ minute returns.

    This is a fallback for historical days where only QQQ exists in ClickHouse.
    It is only meant to compare *entry/exit logic* (timing/reasons), not produce
    accurate dollar PnL.
    """
    minutes = sorted(qqq.keys())
    if len(minutes) < 2:
        return {"QQEW": dict(qqq), "TQQQ": {}, "SQQQ": {}}

    qqew = dict(qqq)
    tqqq: dict[datetime, Bar] = {}
    sqqq: dict[datetime, Bar] = {}

    t_close = float(base_tqqq)
    s_close = float(base_sqqq)
    prev_q_close = float(qqq[minutes[0]].close)

    for m in minutes:
        bq = qqq[m]
        q_close = float(bq.close)
        r = (q_close / prev_q_close - 1.0) if prev_q_close > 0 else 0.0

        t_open = t_close
        s_open = s_close
        t_close = max(0.01, t_close * (1.0 + 3.0 * r))
        s_close = max(0.01, s_close * (1.0 - 3.0 * r))

        # Roughly map QQQ bar shape onto synthetic instruments
        q_up = (float(bq.high) / float(bq.close)) if bq.close > 0 else 1.0
        q_dn = (float(bq.low) / float(bq.close)) if bq.close > 0 else 1.0
        t_high = max(t_open, t_close) * q_up
        t_low = min(t_open, t_close) * q_dn
        s_high = max(s_open, s_close) * q_up
        s_low = min(s_open, s_close) * q_dn

        tqqq[m] = Bar(ts=m, open=t_open, high=t_high, low=t_low, close=t_close, volume=float(bq.volume))
        sqqq[m] = Bar(ts=m, open=s_open, high=s_high, low=s_low, close=s_close, volume=float(bq.volume))
        prev_q_close = q_close

    return {"QQEW": qqew, "TQQQ": tqqq, "SQQQ": sqqq}


def _load_bars_1m_from_clickhouse(day: str, symbols: list[str]) -> dict[str, dict[datetime, Bar]]:
    ch = ClickHouseDB()
    start = _ts_et(day, "09:30")
    end = _ts_et(day, "16:00")
    rows = ch.query(
        """
        SELECT symbol, ts_start, open, high, low, close, volume
        FROM trading.bars_1m_current
        WHERE symbol IN {symbols:Array(String)}
          AND ts_start >= {start:DateTime}
          AND ts_start < {end:DateTime}
        ORDER BY symbol, ts_start
        """,
        parameters={"symbols": symbols, "start": start.replace(tzinfo=None), "end": end.replace(tzinfo=None)},
    )
    out: dict[str, dict[datetime, Bar]] = {s: {} for s in symbols}
    for r in rows:
        sym = str(r["symbol"]).upper()
        ts0 = r["ts_start"]
        if isinstance(ts0, str):
            ts0 = datetime.fromisoformat(ts0)
        if ts0.tzinfo is None:
            ts_et = ts0.replace(tzinfo=ET)
        else:
            ts_et = ts0.astimezone(ET)
        ts_et = _minute(ts_et)
        out.setdefault(sym, {})[ts_et] = Bar(
            ts=ts_et,
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r.get("volume") or 0.0),
        )
    return out


def _load_bars_1m_from_yfinance(day: str, symbols: list[str]) -> dict[str, dict[datetime, Bar]]:
    import yfinance as yf
    import pandas as pd

    d0 = datetime.fromisoformat(day).date()
    start = datetime.combine(d0, time(0, 0), tzinfo=ET)
    end = start + timedelta(days=1)

    out: dict[str, dict[datetime, Bar]] = {s: {} for s in symbols}
    base_index: Optional[list[datetime]] = None

    for sym in symbols:
        df = yf.download(
            sym,
            start=start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d"),
            end=end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d"),
            interval="1m",
            progress=False,
            auto_adjust=False,
            prepost=False,
            threads=False,
        )
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            # yfinance sometimes returns a MultiIndex even for a single ticker.
            # Prefer slicing by ticker level when present.
            if sym in set(df.columns.get_level_values(-1)):
                df = df.xs(sym, axis=1, level=-1, drop_level=True)
            else:
                df.columns = [c[0] for c in df.columns]
        df = df.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)
        df = df[(df.index.time >= time(9, 30)) & (df.index.time <= time(15, 59))]

        # Keep the first successfully loaded full-minute index as base for reindexing sparse symbols like QQEW.
        if base_index is None and len(df) >= 300:
            base_index = [i.to_pydatetime() for i in df.index]

        for ts0, row in df.iterrows():
            ts_et = _minute(ts0.to_pydatetime())
            o = row.get("Open")
            h = row.get("High")
            l = row.get("Low")
            c = row.get("Close")
            v = row.get("Volume", 0.0)
            if pd.isna(v):
                v = 0.0
            out[sym][ts_et] = Bar(
                ts=ts_et,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )

    # Reindex sparse symbols (notably QQEW) to the base minute grid using forward-filled close.
    if base_index is not None:
        for sym in symbols:
            if sym == "QQQ":
                continue
            if not out.get(sym):
                continue
            filled: dict[datetime, Bar] = {}
            last_close: Optional[float] = None
            for m in base_index:
                m = _minute(m.astimezone(ET) if m.tzinfo else m.replace(tzinfo=ET))
                b = out[sym].get(m)
                if b is not None:
                    filled[m] = b
                    last_close = float(b.close)
                elif last_close is not None:
                    filled[m] = Bar(ts=m, open=last_close, high=last_close, low=last_close, close=last_close, volume=0.0)
            out[sym] = filled

    return out


def _agg_1s_to_1m(rows: Iterable[dict[str, Any]], day: str) -> dict[datetime, Bar]:
    want_day = _parse_day(day)
    by_min: dict[datetime, list[dict[str, Any]]] = {}
    for r in rows:
        ts_ms = r.get("ts")
        if ts_ms is None:
            continue
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=ET)
        if dt.date() != want_day:
            continue
        if dt.time() < time(9, 30) or dt.time() >= time(16, 0):
            continue
        m = _minute(dt)
        by_min.setdefault(m, []).append(r)

    out: dict[datetime, Bar] = {}
    for m, rs in by_min.items():
        rs = sorted(rs, key=lambda x: int(x.get("ts") or 0))
        o = float(rs[0].get("open") or rs[0].get("close") or 0.0)
        c = float(rs[-1].get("close") or 0.0)
        h = max(float(x.get("high") or x.get("close") or 0.0) for x in rs)
        l = min(float(x.get("low") or x.get("close") or 0.0) for x in rs)
        v = float(sum(float(x.get("volume") or 0.0) for x in rs))
        out[m] = Bar(ts=m, open=o, high=h, low=l, close=c, volume=v)
    return out


async def _load_bars_1m_from_sfs(day: str, symbols: list[str], n_1s: int = 60_000) -> dict[str, dict[datetime, Bar]]:
    reader = SFS1sReader()
    try:
        rows = await asyncio.gather(*[reader.last_n(sym, n=n_1s) for sym in symbols])
    finally:
        await reader.close()
    out: dict[str, dict[datetime, Bar]] = {}
    for sym, rs in zip(symbols, rows, strict=True):
        out[sym] = _agg_1s_to_1m(rs, day)
    return out


def _lock_params_from_amp(price_base: float, price_0945: float) -> tuple[bool, float, float]:
    if price_base <= 0:
        return True, 0.0, 0.0
    amp = abs(price_0945 - price_base) / price_base
    if amp < 0.001:
        return True, 0.0, 0.0  # deadwater
    if 0.001 <= amp < 0.002:
        return False, 1.2, 0.0012
    if 0.002 <= amp < 0.0045:
        return False, 2.0, 0.0010
    return False, 3.0, 0.0008


def run_backtest_for_day(
    *,
    day: str,
    bars: dict[str, dict[datetime, Bar]],
    atr_seed: dict[str, float],
    morning_end_hhmm: str,
    notional_usd: float,
    counter_guard_mult: Optional[float],
    dev_threshold_mult: Optional[float],
    model: str,
) -> list[Trade]:
    qqq = bars.get("QQQ") or {}
    qqew = bars.get("QQEW") or {}
    tqqq = bars.get("TQQQ") or {}
    sqqq = bars.get("SQQQ") or {}

    minutes = _sorted_minutes(bars)
    if not minutes:
        return []

    start = _ts_et(day, "09:30")
    end = _ts_et(day, morning_end_hhmm)
    minutes = [m for m in minutes if start <= m < end]
    if not minutes:
        return []

    # 09:45 lock based on approximations from 1m data
    b0930 = qqq.get(_ts_et(day, "09:30"))
    b0945 = qqq.get(_ts_et(day, "09:45"))
    if b0930 is None or b0945 is None:
        return []
    price_base = float(b0930.open or b0930.close)
    # Live code locks the 09:45 price using "last" shortly after 09:45,
    # which is typically closer to the 09:45 bar open than the bar close.
    price_0945 = float(b0945.open or b0945.close)
    deadwater, locked_target, locked_dev_threshold = _lock_params_from_amp(price_base, price_0945)
    if deadwater:
        return []

    # Params (defaults match strategies/qqq_intraday_strategy.py for baseline)
    vol_pulse_on = 1.6
    k_normal = 0.40
    k_pulse = 0.30
    atr_cap_mult = 1.8
    tv_min_usd = 0.20
    high_vol_guard = True
    high_vol_ratio = 2.2
    hv_k = 0.50
    hv_tv_min = 0.60
    hv_min_vol_surge = 1.2

    stop_atr_mult = 2.5
    stop_min_abs = 0.50
    momentum_deadzone = 0.05

    atr_seed_t = float(atr_seed.get("TQQQ") or 0.0)
    atr_seed_s = float(atr_seed.get("SQQQ") or 0.0)
    atr_seed_proxy = atr_seed_t

    trade: Optional[Trade] = None
    locked_velocity_sign: Optional[int] = None
    breakeven_done = False
    overext_mark_ts: Optional[datetime] = None
    overext_mark_direction: Optional[str] = None
    prev_vol_surge_ratio: Optional[float] = None

    for m in minutes:
        # Entry is only evaluated in [09:45, 15:30] in live; we backtest morning so still respect 09:45.
        if m.time() < time(9, 45):
            continue

        qqq_now = qqq.get(m)
        qqew_now = qqew.get(m)
        if qqq_now is None or qqew_now is None:
            continue

        qqq_ago = _close_at_or_before(qqq, m - timedelta(minutes=3))
        qqew_ago = _close_at_or_before(qqew, m - timedelta(minutes=3))
        if qqq_ago is None or qqew_ago is None:
            continue

        v3m_signed = float(qqq_now.close - qqq_ago)
        v3m_q = abs(v3m_signed)

        vwap_q = _vwap_up_to(qqq, m)
        vwap_e = _vwap_up_to(qqew, m)
        if vwap_q <= 0 or vwap_e <= 0:
            continue

        atr_live = _atr(qqq, m, 14)
        if atr_live is None or atr_live <= 0 or atr_seed_proxy <= 0:
            continue
        atr_cap = atr_cap_mult * float(atr_seed_proxy)
        atr_eff = min(float(atr_live), float(atr_cap))
        if atr_eff <= 0:
            continue

        # vol_ma10 in live uses previous finalized minutes; approximate using previous 10 minute volumes.
        prev10 = []
        for i in range(1, 11):
            b = qqq.get(m - timedelta(minutes=i))
            if b is not None:
                prev10.append(float(b.volume))
        if len(prev10) < 10:
            continue
        vol_t = float(qqq_now.volume)
        vol_ma10 = float(sum(prev10) / len(prev10))
        vol_surge_ratio = float(vol_t / max(vol_ma10, 1e-9))

        entry_track = ""
        direction: Optional[str] = None
        exec_symbol: Optional[str] = None

        if model == "baseline":
            # Gate 1: direction resonance
            long_dir = (qqq_now.close > vwap_q) and (qqew_now.close > vwap_e)
            short_dir = (qqq_now.close < vwap_q) and (qqew_now.close < vwap_e)
            if not (long_dir or short_dir):
                continue
            direction = "LONG" if long_dir else "SHORT"
            exec_symbol = "TQQQ" if direction == "LONG" else "SQQQ"

            # Gate 1.5: optional VWAP deviation guard (use pre-locked dev threshold bucket)
            if dev_threshold_mult is not None and locked_dev_threshold > 0:
                dev_max = float(dev_threshold_mult) * float(locked_dev_threshold)
                dev = abs(float(qqq_now.close - vwap_q) / float(vwap_q)) if vwap_q > 0 else 0.0
                if dev > dev_max:
                    continue

            # Gate 2: threshold tv
            pulse = vol_surge_ratio >= vol_pulse_on
            k = k_pulse if pulse else k_normal
            tv = max(k * atr_eff, tv_min_usd)
            if v3m_q < tv:
                continue

            # Gate 3: high-vol guard
            if high_vol_guard:
                vol_ratio = float(atr_live) / max(float(atr_seed_proxy), 1e-9)
                if vol_ratio >= high_vol_ratio:
                    if vol_surge_ratio < hv_min_vol_surge:
                        continue
                    tv_hv = max(hv_k * atr_eff, hv_tv_min)
                    if v3m_q < tv_hv:
                        continue
                    tv = tv_hv

            # Counter-momentum guard (optional bolt-on)
            if counter_guard_mult is not None:
                mult = float(counter_guard_mult)
                if direction == "LONG" and v3m_signed < 0 and abs(v3m_signed) >= (mult * tv):
                    continue
                if direction == "SHORT" and v3m_signed > 0 and abs(v3m_signed) >= (mult * tv):
                    continue

        elif model == "gemini_v44_min":
            # Gemini "最小改动版" reinterpreted to fit available 1m data.
            # 1) direction from QQQ vs VWAP_q
            if qqq_now.close < vwap_q:
                direction = "SHORT"
                exec_symbol = "SQQQ"
            elif qqq_now.close > vwap_q:
                direction = "LONG"
                exec_symbol = "TQQQ"
            else:
                continue

            # 2) v3m_signed sign + deadzone (deadzone = 0.15 * ATR_cap_qqq)
            deadzone = 0.15 * float(atr_eff)
            if direction == "LONG" and v3m_signed <= deadzone:
                continue
            if direction == "SHORT" and v3m_signed >= (-deadzone):
                continue

            # 3) momentum level (FAST/CONFIRM)
            v3m_fast = 0.45 * float(atr_eff)
            v3m_confirm_low = 0.30 * float(atr_eff)
            if v3m_q >= v3m_fast:
                entry_track = "FAST"
            elif v3m_q >= v3m_confirm_low:
                entry_track = "CONFIRM"
            else:
                continue

            # 4) volume thresholds (no CV_today available here; use vol_t/vol_ma10)
            if entry_track == "FAST":
                if vol_surge_ratio < 1.35:
                    continue
            else:
                if vol_surge_ratio < 1.55:
                    continue

            # 5) QQEW gate: allow band in FAST, strict in CONFIRM.
            # band_e should be comparable in QQEW dollars; derive as % of QQQ ATR_eff.
            band_pct = (0.10 * float(atr_eff) / float(qqq_now.close)) if qqq_now.close > 0 else 0.0
            band_e = float(vwap_e) * float(band_pct)
            if entry_track == "FAST":
                if direction == "SHORT" and qqew_now.close > (vwap_e + band_e):
                    continue
                if direction == "LONG" and qqew_now.close < (vwap_e - band_e):
                    continue
            else:
                if direction == "SHORT" and qqew_now.close >= vwap_e:
                    continue
                if direction == "LONG" and qqew_now.close <= vwap_e:
                    continue

        elif model in {"elite_a_pullback", "elite_b_decoupled"}:
            if qqq_now.close < vwap_q:
                direction = "SHORT"
                exec_symbol = "SQQQ"
            elif qqq_now.close > vwap_q:
                direction = "LONG"
                exec_symbol = "TQQQ"
            else:
                prev_vol_surge_ratio = vol_surge_ratio
                continue

            deadzone = 0.15 * float(atr_eff)
            v3m_min = 0.35 * float(atr_eff)
            base_max_ext_mult = 1.6
            max_ext_mult = base_max_ext_mult
            if model == "elite_b_decoupled" and prev_vol_surge_ratio is not None:
                if vol_surge_ratio > 1.8 and prev_vol_surge_ratio > 1.8:
                    max_ext_mult = 2.0
            max_ext = float(max_ext_mult) * float(atr_eff)

            if abs(float(qqq_now.close) - float(vwap_q)) > max_ext:
                overext_mark_ts = m
                overext_mark_direction = direction
                prev_vol_surge_ratio = vol_surge_ratio
                continue

            if (
                model == "elite_a_pullback"
                and overext_mark_ts is not None
                and overext_mark_direction == direction
                and (m - overext_mark_ts).total_seconds() <= 90
            ):
                reentry_ext = 1.1 * float(atr_eff)
                if abs(float(qqq_now.close) - float(vwap_q)) > reentry_ext:
                    prev_vol_surge_ratio = vol_surge_ratio
                    continue

            if direction == "LONG" and v3m_signed <= deadzone:
                prev_vol_surge_ratio = vol_surge_ratio
                continue
            if direction == "SHORT" and v3m_signed >= (-deadzone):
                prev_vol_surge_ratio = vol_surge_ratio
                continue
            if v3m_q < v3m_min:
                prev_vol_surge_ratio = vol_surge_ratio
                continue

            if m.time() < time(11, 30):
                t_vol = 1.35
            elif m.time() < time(14, 0):
                t_vol = 1.55
            else:
                t_vol = 1.45
            if v3m_q >= (0.50 * float(atr_eff)):
                t_vol = max(1.0, t_vol - 0.10)
            if vol_surge_ratio < t_vol:
                prev_vol_surge_ratio = vol_surge_ratio
                continue

            if model != "elite_b_decoupled":
                if direction == "SHORT" and not (qqew_now.close < vwap_e):
                    prev_vol_surge_ratio = vol_surge_ratio
                    continue
                if direction == "LONG" and not (qqew_now.close > vwap_e):
                    prev_vol_surge_ratio = vol_surge_ratio
                    continue

            prev_vol_surge_ratio = vol_surge_ratio
        else:
            raise ValueError(f"unknown model={model}")

        # If no trade yet, enter at this minute close
        if trade is None:
            if direction is None or exec_symbol is None:
                continue
            bar_exec = (tqqq.get(m) if exec_symbol == "TQQQ" else sqqq.get(m))
            if bar_exec is None or bar_exec.close <= 0:
                continue
            entry_px = float(bar_exec.close)
            qty = int(math.floor(float(notional_usd) / float(entry_px)))
            if qty <= 0:
                continue

            atr_exec = _atr(tqqq if exec_symbol == "TQQQ" else sqqq, m, 14)
            if atr_exec is None or atr_exec <= 0:
                atr_exec = float(atr_seed_t if exec_symbol == "TQQQ" else atr_seed_s)
            if atr_exec <= 0:
                continue

            dist = max(stop_atr_mult * float(atr_exec), stop_min_abs)
            stop_px = round(entry_px - dist, 2)
            locked_velocity_sign = +1 if direction == "LONG" else -1
            trade = Trade(
                day=day,
                symbol=exec_symbol,
                side=direction,
                qty=qty,
                entry_ts=m,
                entry_px=entry_px,
                stop_px=float(stop_px),
                target=float(locked_target),
                mfe_px=0.0,
                mae_px=0.0,
                entry_track=str(entry_track or ""),
            )
            breakeven_done = False
            continue

        # Manage open position (using minute close)
        if trade.exit_ts is not None:
            continue

        bar_exec = (tqqq.get(m) if trade.symbol == "TQQQ" else sqqq.get(m))
        if bar_exec is None or bar_exec.close <= 0:
            continue
        last_exec = float(bar_exec.close)
        trade.mfe_px = max(float(trade.mfe_px), float(last_exec - trade.entry_px))
        trade.mae_px = min(float(trade.mae_px), float(last_exec - trade.entry_px))

        # 1) target hit
        if trade.target > 0 and last_exec >= (trade.entry_px + trade.target):
            trade.exit_ts = m
            trade.exit_px = last_exec
            trade.exit_reason = "target_hit"
            continue

        # 2) stop hit
        if last_exec <= float(trade.stop_px):
            trade.exit_ts = m
            trade.exit_px = last_exec
            trade.exit_reason = "stop_hit"
            continue

        # 3) breakeven shield
        if not breakeven_done:
            if (last_exec - trade.entry_px) >= 1.0:
                new_stop = round(trade.entry_px + 0.20, 2)
                if new_stop < (last_exec - 0.05):
                    trade.stop_px = float(new_stop)
                    breakeven_done = True
                    continue

        # 5) logic retreat once per minute (we are in minute loop)
        v_q = float(qqq_now.close - qqq_ago)
        v_e = float(qqew_now.close - qqew_ago)
        if abs(v_q) <= momentum_deadzone or abs(v_e) <= momentum_deadzone:
            continue
        if (v_q > 0 and v_e < 0) or (v_q < 0 and v_e > 0):
            continue
        vel_sign = 1 if v_q > 0 else -1
        if locked_velocity_sign is not None and vel_sign != locked_velocity_sign:
            wrong_side = (trade.side == "LONG" and qqq_now.close < vwap_q) or (trade.side == "SHORT" and qqq_now.close > vwap_q)
            if wrong_side:
                trade.exit_ts = m
                trade.exit_px = last_exec
                trade.exit_reason = "logic_retreat"
                continue

    if trade is not None and trade.exit_ts is None:
        m_last = minutes[-1]
        bar_exec = (tqqq.get(m_last) if trade.symbol == "TQQQ" else sqqq.get(m_last))
        if bar_exec is not None and bar_exec.close > 0:
            trade.exit_ts = m_last
            trade.exit_px = float(bar_exec.close)
            trade.exit_reason = "end_of_window"

    return [trade] if trade is not None else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", action="append", required=True, help="Trading day in YYYY-MM-DD (ET). Can repeat.")
    ap.add_argument("--morning-end", default="12:00", help="Morning end time (HH:MM ET).")
    ap.add_argument(
        "--model",
        default="baseline",
        choices=["baseline", "gemini_v44_min", "elite_a_pullback", "elite_b_decoupled"],
    )
    ap.add_argument("--notional-usd", type=float, default=10000.0)
    ap.add_argument(
        "--counter-guard-mult",
        type=float,
        default=None,
        help="Enable counter-momentum guard: opposite-signed v3m_signed with abs(v3m_signed) >= mult*tv blocks entry.",
    )
    ap.add_argument(
        "--dev-threshold-mult",
        type=float,
        default=None,
        help="Enable VWAP deviation guard using locked_dev_threshold bucket: abs((QQQ-vwap)/vwap) <= mult*locked_dev_threshold.",
    )
    ap.add_argument("--out", default="runtime/state/qqq_morning_backtest_report.csv")
    args = ap.parse_args()

    atr_seed_path = Path("runtime/state/qqq_intraday_atr_seed.json")
    if not atr_seed_path.exists():
        raise SystemExit(f"missing {atr_seed_path}")

    report_rows: list[dict[str, Any]] = []
    for day in args.day:
        symbols = ["QQQ", "QQEW", "TQQQ", "SQQQ"]
        use_sfs = (_parse_day(day) == datetime.now(ET).date())
        if use_sfs:
            bars = asyncio.run(_load_bars_1m_from_sfs(day, symbols))
            data_mode = "real_sfs"
        else:
            bars = _load_bars_1m_from_clickhouse(day, symbols)
            data_mode = "real_clickhouse"

            # If ClickHouse does not have all required symbols, fall back to yfinance (recent days).
            missing = [s for s in symbols if not bars.get(s)]
            if missing:
                bars = _load_bars_1m_from_yfinance(day, symbols)
                data_mode = "yfinance_1m"

            # Historical days may only have QQQ in ClickHouse. Build synthetic
            # proxies so we can still validate entry/exit logic timing.
            if bars.get("QQQ") and (not bars.get("QQEW") or not bars.get("TQQQ") or not bars.get("SQQQ")):
                synth = _synth_from_qqq(bars["QQQ"])
                if not bars.get("QQEW"):
                    bars["QQEW"] = synth["QQEW"]
                if not bars.get("TQQQ"):
                    bars["TQQQ"] = synth["TQQQ"]
                if not bars.get("SQQQ"):
                    bars["SQQQ"] = synth["SQQQ"]
                data_mode = "synthetic_from_qqq"

        atr_seed = _load_atr_seed(atr_seed_path, day)
        trades = run_backtest_for_day(
            day=day,
            bars=bars,
            atr_seed=atr_seed,
            morning_end_hhmm=str(args.morning_end),
            notional_usd=float(args.notional_usd),
            counter_guard_mult=args.counter_guard_mult,
            dev_threshold_mult=args.dev_threshold_mult,
            model=str(args.model),
        )

        if not trades:
            report_rows.append(
                {
                    "day": day,
                    "data_mode": data_mode,
                    "model": str(args.model),
                    "symbol": "",
                    "side": "",
                    "qty": 0,
                    "entry_ts": "",
                    "entry_px": "",
                    "stop_px": "",
                    "target": "",
                    "exit_ts": "",
                    "exit_px": "",
                    "exit_reason": "no_trade",
                    "pnl_usd": 0.0,
                    "mfe_px": "",
                    "mae_px": "",
                    "entry_track": "",
                    "counter_guard_mult": args.counter_guard_mult if args.counter_guard_mult is not None else "",
                }
            )
            continue

        for t in trades:
            report_rows.append(
                {
                    "day": day,
                    "data_mode": data_mode,
                    "model": str(args.model),
                    "symbol": t.symbol,
                    "side": t.side,
                    "qty": t.qty,
                    "entry_ts": t.entry_ts.isoformat(),
                    "entry_px": round(float(t.entry_px), 4),
                    "stop_px": round(float(t.stop_px), 4),
                    "target": round(float(t.target), 4),
                    "exit_ts": t.exit_ts.isoformat() if t.exit_ts else "",
                    "exit_px": round(float(t.exit_px or 0.0), 4) if t.exit_px is not None else "",
                    "exit_reason": t.exit_reason or "",
                    "pnl_usd": round(float(t.pnl_usd() or 0.0), 4),
                    "mfe_px": round(float(t.mfe_px), 4),
                    "mae_px": round(float(t.mae_px), 4),
                    "entry_track": str(t.entry_track or ""),
                    "counter_guard_mult": args.counter_guard_mult if args.counter_guard_mult is not None else "",
                }
            )

    out = Path(str(args.out))
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "day",
        "data_mode",
        "model",
        "symbol",
        "side",
        "qty",
        "entry_ts",
        "entry_px",
        "stop_px",
        "target",
        "exit_ts",
        "exit_px",
        "exit_reason",
        "pnl_usd",
        "mfe_px",
        "mae_px",
        "entry_track",
        "counter_guard_mult",
    ]
    with out.open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for r in report_rows:
            wr.writerow(r)

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
