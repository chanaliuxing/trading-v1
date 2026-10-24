from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.dal.sfs_facade import SFS1sReader
from libs.db.ch_pool import get_client

ET = ZoneInfo("America/New_York")


@dataclass
class Tick:
    ts: datetime
    close: float
    volume: float


@dataclass
class Fill:
    ts: datetime
    symbol: str
    side: str
    qty: int
    price: float
    reason: str


def _parse_day(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=ET)


def _load_atr_seed(day: str, path: Path) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rec = (raw.get("days") or {}).get(day) or {}
    out: dict[str, float] = {}
    for sym in ("TQQQ", "SQQQ"):
        v = rec.get(sym)
        if isinstance(v, (int, float)):
            out[sym] = float(v)
    latest = raw.get("latest") or {}
    if str(latest.get("day")) == day and isinstance(latest.get("atr_pre"), dict):
        for sym in ("TQQQ", "SQQQ"):
            if sym not in out and sym in latest["atr_pre"]:
                out[sym] = float(latest["atr_pre"][sym])
    return out


def _atr_1m(closes: list[float], highs: list[float], lows: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: list[float] = []
    for i in range(len(closes) - period, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(float(tr))
    return float(sum(trs) / len(trs)) if trs else None


def _minute_floor(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _vol_threshold(ts: datetime) -> float:
    t = ts.time()
    if time(9, 45) <= t < time(11, 30):
        return 1.35
    if time(11, 30) <= t < time(14, 0):
        return 1.55
    return 1.45


def _is_continuous(window_ts: list[datetime], need_seconds: int = 180, max_gap_s: int = 5) -> bool:
    if len(window_ts) < max(30, int(need_seconds * 0.6)):
        return False
    if (window_ts[-1] - window_ts[0]).total_seconds() < (need_seconds - 2):
        return False
    prev = window_ts[0]
    for cur in window_ts[1:]:
        if (cur - prev).total_seconds() > max_gap_s:
            return False
        prev = cur
    return True


async def _load_ticks_sfs(day: str, n: int) -> dict[str, list[Tick]]:
    reader = SFS1sReader()
    symbols = ["QQQ", "QQEW", "TQQQ", "SQQQ"]
    rows_by_symbol = await asyncio.gather(*[reader.last_n(sym, n=n) for sym in symbols])
    await reader.close()

    d = datetime.fromisoformat(day).date()
    out: dict[str, list[Tick]] = {s: [] for s in symbols}
    for sym, rows in zip(symbols, rows_by_symbol, strict=True):
        for r in reversed(rows):
            ts_ms = int(r.get("ts") or 0)
            if ts_ms <= 0:
                continue
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
            if ts.date() != d:
                continue
            if ts.time() < time(9, 30) or ts.time() > time(15, 59, 59):
                continue
            px = float(r.get("close") or 0.0)
            if px <= 0:
                continue
            vol = float(r.get("volume") or 0.0)
            out[sym].append(Tick(ts=ts, close=px, volume=vol))
    return out


def _load_ticks_5s_clickhouse(day: str) -> dict[str, list[Tick]]:
    symbols = ["QQQ", "QQEW", "TQQQ", "SQQQ"]
    out: dict[str, list[Tick]] = {s: [] for s in symbols}
    client = get_client()
    for sym in symbols:
        rows = client.query(
            """
            SELECT ts_start, close, volume
            FROM bars_5s_ibkr
            WHERE symbol = %(symbol)s
              AND d = toDate(%(day)s)
              AND minute_of_day BETWEEN 570 AND 959
            ORDER BY ts_start ASC
            """,
            {"symbol": sym, "day": day},
        )
        for row in rows:
            ts_raw = row.get("ts_start")
            ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            else:
                ts = ts.astimezone(ET)
            px = float(row.get("close") or 0.0)
            if px <= 0:
                continue
            vol = float(row.get("volume") or 0.0)
            out[sym].append(Tick(ts=ts, close=px, volume=vol))
    return out


async def _load_ticks(day: str, n: int) -> tuple[dict[str, list[Tick]], str]:
    out = await _load_ticks_sfs(day, n)
    if all(out.get(sym) for sym in ("QQQ", "QQEW", "TQQQ", "SQQQ")):
        return out, "sfs_1s"
    out_ch = _load_ticks_5s_clickhouse(day)
    if all(out_ch.get(sym) for sym in ("QQQ", "QQEW", "TQQQ", "SQQQ")):
        return out_ch, "clickhouse_5s"
    return out, "sfs_1s_partial"


def run_backtest(
    day: str,
    ticks: dict[str, list[Tick]],
    atr_seed: dict[str, float],
    notional: float,
    elite_max_ext_mult: float = 1.30,
    *,
    include_qqew_continuity: bool = True,
    qqew_max_stale_sec: float = 5.0,
    ignore_qqew_resonance: bool = False,
    enable_pullback_reentry: bool = False,
    reentry_window_sec: float = 90.0,
    reentry_ext_mult: float = 1.10,
    persist_curr_min: float = 1.35,
    persist_prev_min: float = 1.25,
    burst_vol_min: float = 1.8,
    burst_ext_mult: float = 2.0,
) -> tuple[list[Fill], dict[str, Any]]:
    qqq = ticks["QQQ"]
    qqew = ticks["QQEW"]
    tqqq = ticks["TQQQ"]
    sqqq = ticks["SQQQ"]
    if not qqq or not qqew or not tqqq or not sqqq:
        return [], {"status": "no_data"}

    idx_e = 0
    idx_t = 0
    idx_s = 0
    qqq_hist: list[Tick] = []
    qqew_hist: list[Tick] = []
    tqqq_hist: list[Tick] = []
    sqqq_hist: list[Tick] = []

    fills: list[Fill] = []
    direction: Optional[str] = None
    symbol: Optional[str] = None
    qty_total = 0
    qty_left = 0
    entry_px = 0.0
    entry_ts: Optional[datetime] = None
    hard_stop = 0.0
    tp_done = False
    trail_stop: Optional[float] = None
    peak_px = 0.0
    atr_cap_entry = 0.0
    v3m_hist: list[float] = []
    gate_blocked: Counter[str] = Counter()
    overext_mark_ts: Optional[datetime] = None
    overext_mark_direction: Optional[str] = None
    prev_vol_ratio: Optional[float] = None

    # 1m bars for QQQ ATR
    m_open: dict[datetime, float] = {}
    m_high: dict[datetime, float] = {}
    m_low: dict[datetime, float] = {}
    m_close: dict[datetime, float] = {}
    m_vol: dict[datetime, float] = {}

    q_pv = 0.0
    q_v = 0.0
    e_pv = 0.0
    e_v = 0.0

    atr_seed_proxy = float(atr_seed.get("TQQQ") or 0.0)
    atr_cap_mult = 1.8

    for q in qqq:
        ts = q.ts
        if ts.time() < time(9, 30) or ts.time() > time(15, 30):
            continue

        while idx_e + 1 < len(qqew) and qqew[idx_e + 1].ts <= ts:
            idx_e += 1
        while idx_t + 1 < len(tqqq) and tqqq[idx_t + 1].ts <= ts:
            idx_t += 1
        while idx_s + 1 < len(sqqq) and sqqq[idx_s + 1].ts <= ts:
            idx_s += 1
        e = qqew[idx_e]
        t = tqqq[idx_t]
        s = sqqq[idx_s]
        age_qqew_sec = max(0.0, (ts - e.ts).total_seconds())
        age_qqq_sec = max(0.0, (ts - q.ts).total_seconds())
        if age_qqq_sec > 5.0:
            gate_blocked["qqq_stale"] += 1
            continue
        if age_qqew_sec > float(qqew_max_stale_sec):
            gate_blocked["qqew_stale"] += 1
            continue

        qqq_hist.append(q)
        qqew_hist.append(e)
        tqqq_hist.append(t)
        sqqq_hist.append(s)

        q_pv += q.close * max(0.0, q.volume)
        q_v += max(0.0, q.volume)
        e_pv += e.close * max(0.0, e.volume)
        e_v += max(0.0, e.volume)
        if q_v <= 0 or e_v <= 0:
            gate_blocked["vwap_not_ready"] += 1
            continue
        q_vwap = q_pv / q_v
        e_vwap = e_pv / e_v

        m = _minute_floor(ts)
        if m not in m_open:
            m_open[m] = q.close
            m_high[m] = q.close
            m_low[m] = q.close
            m_close[m] = q.close
            m_vol[m] = max(0.0, q.volume)
        else:
            m_high[m] = max(m_high[m], q.close)
            m_low[m] = min(m_low[m], q.close)
            m_close[m] = q.close
            m_vol[m] += max(0.0, q.volume)

        minute_keys = sorted(m_close.keys())
        closes = [m_close[k] for k in minute_keys]
        highs = [m_high[k] for k in minute_keys]
        lows = [m_low[k] for k in minute_keys]
        atr_live = _atr_1m(closes, highs, lows, 14)
        if atr_live is None or atr_seed_proxy <= 0:
            gate_blocked["atr_not_ready"] += 1
            continue
        atr_cap = min(float(atr_live), float(atr_cap_mult * atr_seed_proxy))
        if atr_cap <= 0:
            gate_blocked["atr_cap_invalid"] += 1
            continue

        # v3m_signed from 180s ago
        base_idx = None
        for i in range(len(qqq_hist) - 1, -1, -1):
            if (ts - qqq_hist[i].ts).total_seconds() >= 180:
                base_idx = i
                break
        if base_idx is None:
            gate_blocked["v3m_not_ready"] += 1
            continue
        v3m_signed = float(q.close - qqq_hist[base_idx].close)
        v3m_abs = abs(v3m_signed)
        v3m_hist.append(v3m_abs)

        # continuity check
        q_win = [x.ts for x in qqq_hist if (ts - x.ts).total_seconds() <= 180]
        e_win = [x.ts for x in qqew_hist if (ts - x.ts).total_seconds() <= 180]
        qqq_ok = _is_continuous(q_win, 180, 5)
        qqew_ok = _is_continuous(e_win, 180, int(max(5.0, float(qqew_max_stale_sec))))
        if not qqq_ok or (include_qqew_continuity and not qqew_ok):
            gate_blocked["continuity_block"] += 1
            continue

        prev_minutes = [k for k in minute_keys if k < m][-10:]
        vol_surge_ratio = 0.0
        prev_ratio_for_gate = prev_vol_ratio
        if len(prev_minutes) >= 10:
            vol_ma10_tmp = sum(m_vol[k] for k in prev_minutes) / 10.0
            vol_surge_ratio = float(m_vol[m] / max(1e-9, vol_ma10_tmp))
            prev_vol_ratio = vol_surge_ratio

        deadzone = 0.15 * atr_cap
        v3m_min = 0.35 * atr_cap
        max_ext = float(elite_max_ext_mult) * atr_cap
        if prev_ratio_for_gate is not None:
            if vol_surge_ratio > float(burst_vol_min) and prev_ratio_for_gate > float(burst_vol_min):
                max_ext = max(max_ext, float(burst_ext_mult) * atr_cap)
            elif vol_surge_ratio >= float(persist_curr_min) and prev_ratio_for_gate >= float(persist_prev_min):
                max_ext = max(max_ext, float(elite_max_ext_mult) * atr_cap)

        # Entry
        if qty_left == 0 and fills == []:
            if ts.time() < time(9, 45):
                gate_blocked["time_window_block"] += 1
                continue

            direction_now: Optional[str] = None
            if q.close < q_vwap:
                direction_now = "SHORT"
            elif q.close > q_vwap:
                direction_now = "LONG"
            if direction_now is None:
                gate_blocked["direction_mismatch"] += 1
                continue

            # Gate 1: position + extension
            if abs(q.close - q_vwap) > max_ext:
                overext_mark_ts = ts
                overext_mark_direction = direction_now
                gate_blocked["elite_overextended"] += 1
                continue
            if (
                enable_pullback_reentry
                and overext_mark_ts is not None
                and overext_mark_direction == direction_now
                and (ts - overext_mark_ts).total_seconds() <= float(reentry_window_sec)
            ):
                reentry_ext = float(reentry_ext_mult) * atr_cap
                if abs(q.close - q_vwap) > reentry_ext:
                    gate_blocked["elite_reentry_wait_pullback"] += 1
                    continue

            # Gate 2: sign consistency
            if direction_now == "SHORT" and not (v3m_signed < -deadzone):
                gate_blocked["elite_sign_mismatch"] += 1
                continue
            if direction_now == "LONG" and not (v3m_signed > deadzone):
                gate_blocked["elite_sign_mismatch"] += 1
                continue
            if v3m_abs < v3m_min:
                gate_blocked["velocity_too_low"] += 1
                continue

            # Gate 3: non-decay on last 3s
            last3 = v3m_hist[-3:] if len(v3m_hist) >= 3 else v3m_hist
            if not last3:
                gate_blocked["elite_decay_window_insufficient"] += 1
                continue
            if v3m_abs < (max(last3) * 0.95):
                gate_blocked["elite_momentum_decay"] += 1
                continue

            # Gate 4: time-volume
            if len(prev_minutes) < 10:
                gate_blocked["volume_history_insufficient"] += 1
                continue
            vol_ratio = vol_surge_ratio
            t_vol = _vol_threshold(ts)
            if v3m_abs >= (0.50 * atr_cap):
                t_vol -= 0.10
            if vol_ratio < t_vol:
                gate_blocked["elite_volume_too_low"] += 1
                continue

            # Gate 5: QQEW resonance + high-vol 2 samples
            if not ignore_qqew_resonance:
                if direction_now == "SHORT" and not (e.close < e_vwap):
                    gate_blocked["qqew_direction_mismatch"] += 1
                    continue
                if direction_now == "LONG" and not (e.close > e_vwap):
                    gate_blocked["qqew_direction_mismatch"] += 1
                    continue
            high_vol = (atr_live / max(1e-9, atr_seed_proxy)) > 1.5
            if high_vol and len(qqq_hist) >= 2:
                prev_ts = qqq_hist[-2].ts
                prev_base_idx = None
                for i in range(len(qqq_hist) - 2, -1, -1):
                    if (prev_ts - qqq_hist[i].ts).total_seconds() >= 180:
                        prev_base_idx = i
                        break
                if prev_base_idx is None:
                    gate_blocked["elite_high_vol_sample_missing"] += 1
                    continue
                prev_v3m = float(qqq_hist[-2].close - qqq_hist[prev_base_idx].close)
                if direction_now == "SHORT" and not (prev_v3m < 0 and v3m_signed < 0):
                    gate_blocked["elite_high_vol_sign_check_failed"] += 1
                    continue
                if direction_now == "LONG" and not (prev_v3m > 0 and v3m_signed > 0):
                    gate_blocked["elite_high_vol_sign_check_failed"] += 1
                    continue

            symbol_now = "SQQQ" if direction_now == "SHORT" else "TQQQ"
            px = s.close if symbol_now == "SQQQ" else t.close
            qn = int(math.floor(notional / max(1e-9, px)))
            if qn <= 0:
                gate_blocked["qty_zero"] += 1
                continue
            direction = direction_now
            symbol = symbol_now
            qty_total = qn
            qty_left = qn
            entry_px = float(px)
            entry_ts = ts
            atr_cap_entry = float(atr_cap)
            hard_stop = entry_px - atr_cap_entry
            peak_px = entry_px
            fills.append(Fill(ts=ts, symbol=symbol, side="BUY", qty=qty_total, price=entry_px, reason="entry"))
            overext_mark_ts = None
            overext_mark_direction = None
            continue

        # Manage position
        if qty_left > 0 and symbol and direction and entry_ts:
            cur_px = s.close if symbol == "SQQQ" else t.close
            peak_px = max(peak_px, cur_px)
            profit_px = cur_px - entry_px
            holding_sec = (ts - entry_ts).total_seconds()

            # hard stop
            if cur_px <= hard_stop:
                fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=qty_left, price=cur_px, reason="hard_stop"))
                qty_left = 0
                continue

            # structure invalid
            if direction == "SHORT" and (v3m_signed > deadzone or q.close > q_vwap):
                fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=qty_left, price=cur_px, reason="structure_invalid"))
                qty_left = 0
                continue
            if direction == "LONG" and (v3m_signed < -deadzone or q.close < q_vwap):
                fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=qty_left, price=cur_px, reason="structure_invalid"))
                qty_left = 0
                continue

            # time protection
            if holding_sec > 600 and profit_px < 0.40:
                fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=qty_left, price=cur_px, reason="time_protect"))
                qty_left = 0
                continue

            # partial take profit 80%
            tp_trigger = max(1.0, 1.5 * atr_cap_entry)
            if (not tp_done) and profit_px >= tp_trigger:
                q_take = max(1, int(round(qty_total * 0.8)))
                q_take = min(q_take, qty_left)
                fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=q_take, price=cur_px, reason="tp80"))
                qty_left -= q_take
                tp_done = True
                trail_stop = max(entry_px + 0.01, cur_px - (0.3 * atr_cap_entry))
                continue

            # trailing on remaining 20%
            if tp_done and qty_left > 0:
                trail_now = peak_px - (0.3 * atr_cap_entry)
                trail_stop = max(trail_stop or -1e9, trail_now)
                if profit_px >= 1.80:
                    fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=qty_left, price=cur_px, reason="tp_cap_1p8"))
                    qty_left = 0
                    continue
                if cur_px <= (trail_stop or -1e9):
                    fills.append(Fill(ts=ts, symbol=symbol, side="SELL", qty=qty_left, price=cur_px, reason="trail_stop"))
                    qty_left = 0
                    continue

    # force close at end of data
    if qty_left > 0 and symbol and (tqqq_hist or sqqq_hist):
        last_ts = (sqqq_hist[-1].ts if symbol == "SQQQ" else tqqq_hist[-1].ts)
        last_px = (sqqq_hist[-1].close if symbol == "SQQQ" else tqqq_hist[-1].close)
        fills.append(Fill(ts=last_ts, symbol=symbol, side="SELL", qty=qty_left, price=last_px, reason="end_of_data"))
        qty_left = 0

    # summary
    realized = 0.0
    if fills:
        buy_value = sum(f.qty * f.price for f in fills if f.side == "BUY")
        sell_value = sum(f.qty * f.price for f in fills if f.side == "SELL")
        realized = sell_value - buy_value
    summary = {
        "status": "ok" if fills else "no_trade",
        "fills": len(fills),
        "realized_pnl_usd": round(realized, 4),
        "elite_max_ext_mult": float(elite_max_ext_mult),
        "top_blocked": gate_blocked.most_common(8),
    }
    return fills, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    ap.add_argument("--notional-usd", type=float, default=10_000.0)
    ap.add_argument("--sfs-n", type=int, default=80_000)
    ap.add_argument("--elite-max-ext-mult", type=float, default=1.30)
    ap.add_argument("--profile", choices=["baseline", "a_pullback", "b_decoupled"], default="baseline")
    ap.add_argument("--out", default="runtime/state/qqq_v44_elite_backtest_today.csv")
    args = ap.parse_args()

    day = str(args.day)
    atr_seed_path = Path("runtime/state/qqq_intraday_atr_seed.json")
    atr_seed = _load_atr_seed(day, atr_seed_path) if atr_seed_path.exists() else {}
    ticks, data_source = asyncio.run(_load_ticks(day, int(args.sfs_n)))
    profile = str(args.profile)
    run_kwargs: dict[str, Any] = {
        "elite_max_ext_mult": float(args.elite_max_ext_mult),
        "include_qqew_continuity": True,
        "qqew_max_stale_sec": 5.0,
        "ignore_qqew_resonance": False,
        "enable_pullback_reentry": False,
        "reentry_window_sec": 90.0,
        "reentry_ext_mult": 1.10,
        "persist_curr_min": 1.35,
        "persist_prev_min": 1.25,
        "burst_vol_min": 1.8,
        "burst_ext_mult": 2.0,
    }
    if profile == "a_pullback":
        run_kwargs.update(
            {
                "elite_max_ext_mult": max(1.6, float(args.elite_max_ext_mult)),
                "include_qqew_continuity": True,
                "qqew_max_stale_sec": 8.0,
                "ignore_qqew_resonance": False,
                "enable_pullback_reentry": True,
                "reentry_window_sec": 90.0,
                "reentry_ext_mult": 1.10,
            }
        )
    elif profile == "b_decoupled":
        run_kwargs.update(
            {
                "elite_max_ext_mult": max(1.6, float(args.elite_max_ext_mult)),
                "include_qqew_continuity": False,
                "qqew_max_stale_sec": 15.0,
                "ignore_qqew_resonance": True,
                "enable_pullback_reentry": False,
                "persist_curr_min": 1.35,
                "persist_prev_min": 1.25,
                "burst_vol_min": 1.8,
                "burst_ext_mult": 2.0,
            }
        )
    fills, summary = run_backtest(
        day,
        ticks,
        atr_seed,
        float(args.notional_usd),
        **run_kwargs,
    )
    summary["profile"] = profile
    summary["data_source"] = data_source

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["ts_et", "symbol", "side", "qty", "price", "reason"])
        wr.writeheader()
        for x in fills:
            wr.writerow(
                {
                    "ts_et": x.ts.isoformat(),
                    "symbol": x.symbol,
                    "side": x.side,
                    "qty": x.qty,
                    "price": round(x.price, 4),
                    "reason": x.reason,
                }
            )

    print(out)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
