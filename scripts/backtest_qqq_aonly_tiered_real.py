from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backtest_qqq_ab_real_ticks import ET, Tick, _load_ticks
from scripts.backtest_qqq_v1_morning import (
    _atr,
    _close_at_or_before,
    _load_atr_seed,
    _load_bars_1m_from_clickhouse,
    _load_bars_1m_from_sfs,
    _synth_from_qqq,
    _vwap_up_to,
)


@dataclass
class EntryResult:
    day: str
    data_source: str
    traded: bool
    tier: str
    path: str
    direction: str
    entry_ts: str
    entry_px_underlying: float
    entry_dev_atr: float
    blocks_top: list[tuple[str, int]]


def _value_at_or_before(series: deque[tuple[datetime, float]], ts: datetime) -> Optional[float]:
    for t, v in reversed(series):
        if t <= ts:
            return float(v)
    return None


def _seconds_hold(
    series: deque[tuple[datetime, float]],
    now: datetime,
    *,
    vwap: float,
    side: str,
    seconds: int,
) -> bool:
    cutoff = now - timedelta(seconds=seconds)
    points = [v for t, v in series if t >= cutoff]
    if not points:
        return False
    if side == "LONG":
        return all(float(v) > float(vwap) for v in points)
    return all(float(v) < float(vwap) for v in points)


def _last_n_samples_same_side(
    series: deque[tuple[datetime, float]],
    *,
    vwap: float,
    side: str,
    n: int,
) -> bool:
    if len(series) < n:
        return False
    vals = [float(v) for _, v in list(series)[-n:]]
    if side == "LONG":
        return all(v > float(vwap) for v in vals)
    return all(v < float(vwap) for v in vals)


def _load_day_exec_bars(day: str) -> tuple[dict[str, dict[datetime, Any]], str]:
    syms = ["QQQ", "QQEW", "TQQQ", "SQQQ"]
    bars = asyncio.run(_load_bars_1m_from_sfs(day, syms, n_1s=120000))
    src = "sfs_1s"
    if not bars.get("QQQ"):
        bars = _load_bars_1m_from_clickhouse(day, syms)
        src = "clickhouse_1m"
    if bars.get("QQQ") and (not bars.get("QQEW") or not bars.get("TQQQ") or not bars.get("SQQQ")):
        synth = _synth_from_qqq(bars["QQQ"])
        for k, v in synth.items():
            if not bars.get(k):
                bars[k] = v
    return bars, src


def _exit_for_entry(
    *,
    day: str,
    direction: str,
    entry_ts: datetime,
    extras: dict[str, Any],
    bars: dict[str, dict[datetime, Any]],
) -> dict[str, Any]:
    qqq = bars.get("QQQ") or {}
    qqew = bars.get("QQEW") or {}
    symbol = "TQQQ" if direction == "LONG" else "SQQQ"
    exe = bars.get(symbol) or {}
    if not exe:
        return {"error": "missing_exec_bars"}

    if entry_ts not in exe:
        entry_ts = entry_ts.replace(second=0, microsecond=0)
    if entry_ts not in exe:
        return {"error": "entry_ts_not_in_exec_bars"}

    minutes = sorted([m for m in exe if m >= entry_ts])
    if not minutes:
        return {"error": "no_minutes_after_entry"}

    m0930 = entry_ts.replace(hour=9, minute=30, second=0, microsecond=0)
    m0945 = entry_ts.replace(hour=9, minute=45, second=0, microsecond=0)
    base_px = _close_at_or_before(qqq, m0930) or list(qqq.values())[0].close
    px0945 = _close_at_or_before(qqq, m0945) or base_px
    amp = abs(float(px0945) - float(base_px)) / float(base_px) if float(base_px) > 0 else 0.0
    tlo = float(extras.get("target_low_amp", 1.2))
    tmid = float(extras.get("target_mid_amp", 2.0))
    thi = float(extras.get("target_high_amp", 3.0))
    if amp < 0.001:
        target = 0.0
    elif amp < 0.002:
        target = tlo
    elif amp < 0.0045:
        target = tmid
    else:
        target = thi

    entry_px = float(exe[entry_ts].close)
    qty = int(float(extras.get("notional_usd", 10000)) // entry_px)
    atr_seed = _load_atr_seed(Path("runtime/state/qqq_intraday_atr_seed.json"), day)
    atr_exec = _atr(exe, entry_ts, 14)
    if not atr_exec or atr_exec <= 0:
        atr_exec = float(atr_seed.get(symbol, 0.0))
    stop_dist = max(
        float(extras.get("stop_atr_mult", 2.5)) * float(atr_exec),
        float(extras.get("stop_min_abs", 0.5)),
    )
    stop_px = round(entry_px - stop_dist, 2)
    init_stop = stop_px

    be_profit = float(extras.get("break_even_profit_abs", 1.0))
    be_off = float(extras.get("break_even_offset_abs", 0.2))
    mdz = float(extras.get("momentum_deadzone", 0.05))
    logic_retreat_min_hold_sec = float(extras.get("logic_retreat_min_hold_sec", 0))
    lock_sign = 1 if direction == "LONG" else -1
    be = False

    for m in minutes:
        px = float(exe[m].close)
        if target > 0 and px >= entry_px + target:
            return {
                "exec_symbol": symbol,
                "exec_entry_px": round(entry_px, 4),
                "qty": qty,
                "target": target,
                "stop_px_initial": round(init_stop, 4),
                "exit_ts": m.isoformat(),
                "exit_px": round(px, 4),
                "exit_reason": "target_hit",
                "pnl_usd": round((px - entry_px) * qty, 2),
            }
        if px <= stop_px:
            return {
                "exec_symbol": symbol,
                "exec_entry_px": round(entry_px, 4),
                "qty": qty,
                "target": target,
                "stop_px_initial": round(init_stop, 4),
                "exit_ts": m.isoformat(),
                "exit_px": round(px, 4),
                "exit_reason": "stop_hit",
                "pnl_usd": round((px - entry_px) * qty, 2),
            }

        if (not be) and (px - entry_px) >= be_profit:
            ns = round(entry_px + be_off, 2)
            if ns < (px - 0.05):
                stop_px = ns
                be = True
                continue

        q = qqq.get(m)
        e = qqew.get(m)
        qago = _close_at_or_before(qqq, m - timedelta(minutes=3))
        eago = _close_at_or_before(qqew, m - timedelta(minutes=3))
        if not q or not e or qago is None or eago is None:
            continue
        vq = float(q.close) - float(qago)
        ve = float(e.close) - float(eago)
        if abs(vq) <= mdz or abs(ve) <= mdz:
            continue
        if (vq > 0 and ve < 0) or (vq < 0 and ve > 0):
            continue
        vsgn = 1 if vq > 0 else -1
        if vsgn != lock_sign:
            held_sec = max(0.0, (m - entry_ts).total_seconds())
            if held_sec < logic_retreat_min_hold_sec:
                continue
            vwap_q = _vwap_up_to(qqq, m)
            wrong_side = (direction == "LONG" and float(q.close) < vwap_q) or (
                direction == "SHORT" and float(q.close) > vwap_q
            )
            if wrong_side:
                return {
                    "exec_symbol": symbol,
                    "exec_entry_px": round(entry_px, 4),
                    "qty": qty,
                    "target": target,
                    "stop_px_initial": round(init_stop, 4),
                    "exit_ts": m.isoformat(),
                    "exit_px": round(px, 4),
                    "exit_reason": "logic_retreat",
                    "pnl_usd": round((px - entry_px) * qty, 2),
                }

    m = minutes[-1]
    px = float(exe[m].close)
    return {
        "exec_symbol": symbol,
        "exec_entry_px": round(entry_px, 4),
        "qty": qty,
        "target": target,
        "stop_px_initial": round(init_stop, 4),
        "exit_ts": m.isoformat(),
        "exit_px": round(px, 4),
        "exit_reason": "end_of_window",
        "pnl_usd": round((px - entry_px) * qty, 2),
    }


def run_day_aonly_tiered(
    *,
    day: str,
    ticks: dict[str, list[Tick]],
    data_source: str,
    atr_seed_tqqq: float,
    trade_window_start: time = time(9, 32),
    trade_window_end: time = time(15, 30),
    entry_cooloff_end: time = time(9, 30, 0),
    qqq_max_age_sec: float = 15.0,
    qqq_trade_allow_age_sec_tier1: float = 3.0,
    qqq_trade_allow_age_sec_tier2: float = 90.0,
    atr_cap_mult: float = 1.8,
    a_vol_min_base: float = 0.0,
    a_pullback_mult_open: float = 1.4,
    a_pullback_mult_late: float = 1.5,
    a_pullback_switch_time: time = time(11, 0),
    or_throttle_enable: bool = False,
    or_start: time = time(9, 30),
    or_end: time = time(9, 35),
    or_width_atr_mult: float = 0.8,
    or_add_vol_min: float = 0.1,
    or_add_pullback_mult: float = 0.1,
    early_lock_enable: bool = False,
    early_lock_start: time = time(9, 32),
    early_lock_end: time = time(10, 5),
    early_lock_trigger_live_atr: float = 1.2,
    early_reverse_veto: bool = True,
    opening_mode_end: time = time(9, 40, 0),
    opening_breakout_vol_min: float = 1.2,
) -> EntryResult:
    qqq = ticks.get("QQQ") or []
    qqew = ticks.get("QQEW") or []
    if not qqq:
        return EntryResult(
            day=day,
            data_source=data_source,
            traded=False,
            tier="",
            path="",
            direction="",
            entry_ts="",
            entry_px_underlying=0.0,
            entry_dev_atr=0.0,
            blocks_top=[("no_data", 1)],
        )

    qqew_by_ts = {t.ts: t for t in qqew}
    blocks: Counter[str] = Counter()

    q_series: deque[tuple[datetime, float]] = deque(maxlen=30_000)
    m_close: deque[float] = deque(maxlen=400)
    m_high: deque[float] = deque(maxlen=400)
    m_low: deque[float] = deque(maxlen=400)
    m_vol: deque[float] = deque(maxlen=400)
    m_ts: deque[datetime] = deque(maxlen=400)
    cur_min: Optional[datetime] = None
    cur_o = cur_h = cur_l = cur_c = cur_v = 0.0

    vwap_num = 0.0
    vwap_den = 0.0
    low_tag_ts: Optional[datetime] = None
    high_tag_ts: Optional[datetime] = None
    opening_prices: list[float] = []
    opening_width: Optional[float] = None
    throttle_applied = False
    effective_vol_min = float(a_vol_min_base)
    effective_pull_add = 0.0
    open_ref_px: Optional[float] = None
    early_lock_direction: Optional[str] = None

    def finalize_minute() -> None:
        nonlocal cur_min, cur_h, cur_l, cur_c, cur_v
        if cur_min is None:
            return
        m_ts.append(cur_min)
        m_close.append(cur_c)
        m_high.append(cur_h)
        m_low.append(cur_l)
        m_vol.append(cur_v)

    for i, q in enumerate(qqq):
        now = q.ts
        if or_start <= now.time() <= or_end:
            opening_prices.append(float(q.close))

        if now.time() < trade_window_start or now.time() > trade_window_end:
            continue
        if now.time() < entry_cooloff_end:
            blocks["entry_cooloff"] += 1
            continue

        # update series and vwap
        q_series.append((now, float(q.close)))
        vwap_num += float(q.close) * max(0.0, float(q.volume))
        vwap_den += max(0.0, float(q.volume))
        if vwap_den <= 0:
            blocks["vwap_not_ready"] += 1
            continue
        vwap = vwap_num / vwap_den

        # minute agg
        minute = now.replace(second=0, microsecond=0)
        if cur_min is None:
            cur_min = minute
            cur_o = cur_h = cur_l = cur_c = float(q.close)
            cur_v = float(q.volume)
        elif minute == cur_min:
            px = float(q.close)
            cur_h = max(cur_h, px)
            cur_l = min(cur_l, px)
            cur_c = px
            cur_v += float(q.volume)
        else:
            finalize_minute()
            cur_min = minute
            cur_o = cur_h = cur_l = cur_c = float(q.close)
            cur_v = float(q.volume)

        # tier selection
        if data_source in {"sfs_1s", "clickhouse_5s"}:
            tier = "T1"
        elif data_source == "clickhouse_1m":
            tier = "T2"
        else:
            tier = "T3"

        # age gate (tier-aware)
        prev_ts = qqq[i - 1].ts if i > 0 else now
        age_sec = max(0.0, (now - prev_ts).total_seconds())
        max_age = qqq_max_age_sec if tier == "T1" else max(qqq_max_age_sec, qqq_trade_allow_age_sec_tier2)
        if age_sec > max_age:
            blocks["data_too_stale_qqq"] += 1
            continue

        # ATR proxy
        atr_1m = _atr_1m_local(list(m_close), list(m_high), list(m_low), 14)
        range_14 = 0.0
        if len(m_high) >= 14:
            highs_14 = list(m_high)[-14:]
            lows_14 = list(m_low)[-14:]
            range_14 = max(highs_14) - min(lows_14)
        atr_proxy = float(atr_1m or max(0.35, range_14))
        if atr_proxy <= 0:
            atr_proxy = max(0.35, float(atr_seed_tqqq))
        atr_cap = atr_proxy * float(atr_cap_mult)

        px_now = float(q.close)
        dev = abs(px_now - float(vwap))
        if (not throttle_applied) and or_throttle_enable and now.time() >= or_end and len(opening_prices) >= 2:
            opening_width = max(opening_prices) - min(opening_prices)
            if float(opening_width) < float(or_width_atr_mult) * float(max(atr_proxy, 1e-9)):
                effective_vol_min += float(or_add_vol_min)
                effective_pull_add += float(or_add_pullback_mult)
            throttle_applied = True

        pull_mult = float(a_pullback_mult_open if now.time() <= a_pullback_switch_time else a_pullback_mult_late) + float(effective_pull_add)

        is_opening_mode = now.time() < opening_mode_end
        min_vol_required = float(effective_vol_min)
        if is_opening_mode:
            min_vol_required = max(min_vol_required, float(opening_breakout_vol_min))

        if min_vol_required > 0.0:
            if len(m_vol) < 10:
                blocks["vol_not_ready"] += 1
                continue
            vol_ma10 = sum(list(m_vol)[-10:]) / 10.0
            vol_ratio = float(m_vol[-1]) / max(float(vol_ma10), 1e-9)
            if vol_ratio < float(min_vol_required):
                blocks["opening_vol_below_min" if is_opening_mode else "vol_below_min"] += 1
                continue

        if early_lock_enable and now.time() >= early_lock_start:
            if open_ref_px is None:
                open_ref_px = px_now
            if early_lock_direction is None and now.time() <= early_lock_end:
                atr_for_lock = max(float(atr_proxy), float(atr_seed_tqqq), 1e-9)
                up_move = (px_now - float(open_ref_px)) / atr_for_lock
                dn_move = (float(open_ref_px) - px_now) / atr_for_lock
                if up_move >= float(early_lock_trigger_live_atr):
                    early_lock_direction = "LONG"
                elif dn_move >= float(early_lock_trigger_live_atr):
                    early_lock_direction = "SHORT"

        # Direction gate (tiered)
        direction = ""
        if tier == "T1":
            p30 = _value_at_or_before(q_series, now - timedelta(seconds=30))
            p10 = _value_at_or_before(q_series, now - timedelta(seconds=10))
            if p30 is None or p10 is None:
                blocks["dir_not_ready_t1"] += 1
                continue
            if px_now > vwap and (px_now - p30) > 0 and (px_now - p10) >= 0:
                direction = "LONG"
            elif px_now < vwap and (px_now - p30) < 0 and (px_now - p10) <= 0:
                direction = "SHORT"
            else:
                blocks["dir_mismatch_t1"] += 1
                continue
        elif tier == "T2":
            if len(m_close) < 3:
                blocks["dir_not_ready_t2"] += 1
                continue
            c1, c2, c3 = float(m_close[-3]), float(m_close[-2]), float(m_close[-1])
            inc = int(c2 > c1) + int(c3 > c2)
            dec = int(c2 < c1) + int(c3 < c2)
            above = int(c2 > vwap) + int(c3 > vwap)
            below = int(c2 < vwap) + int(c3 < vwap)
            if px_now > vwap and (inc >= 2 or above >= 2):
                direction = "LONG"
            elif px_now < vwap and (dec >= 2 or below >= 2):
                direction = "SHORT"
            else:
                blocks["dir_mismatch_t2"] += 1
                continue
        else:
            if len(q_series) < 3:
                blocks["dir_not_ready_t3"] += 1
                continue
            last3 = [v for _, v in list(q_series)[-3:]]
            above = sum(1 for v in last3 if v > vwap)
            below = sum(1 for v in last3 if v < vwap)
            if above >= 2:
                direction = "LONG"
            elif below >= 2:
                direction = "SHORT"
            else:
                blocks["dir_mismatch_t3"] += 1
                continue

        # Gate A2 max dev
        if dev > float(pull_mult) * float(atr_proxy):
            blocks["pullback_overextended"] += 1
            continue

        if early_lock_enable and early_reverse_veto and early_lock_direction is not None and direction != early_lock_direction:
            blocks["early_reverse_veto"] += 1
            continue

        # Track extremes for reclaim
        cutoff_8m = now - timedelta(minutes=8)
        trailing = [v for t, v in q_series if t >= cutoff_8m]
        if trailing:
            if px_now <= min(trailing):
                low_tag_ts = now
            if px_now >= max(trailing):
                high_tag_ts = now

        # confirmation helper
        def confirm_side(side: str) -> bool:
            if tier == "T1":
                hold = 10 if side == "LONG" else 10
                return _seconds_hold(q_series, now, vwap=vwap, side=side, seconds=hold)
            if tier == "T2":
                if len(m_close) < 2:
                    return False
                c2, c3 = float(m_close[-2]), float(m_close[-1])
                return (c2 > vwap and c3 > vwap) if side == "LONG" else (c2 < vwap and c3 < vwap)
            return _last_n_samples_same_side(q_series, vwap=vwap, side=side, n=3)

        # A3 ignition (touch + reclaim)
        if tier == "T1":
            lookback = now - timedelta(seconds=60)
        elif tier == "T2":
            lookback = now - timedelta(minutes=3)
        else:
            lookback = now - timedelta(seconds=120)
        near_vwap = any(abs(float(v) - float(vwap)) <= 0.2 * float(atr_proxy) for t, v in q_series if t >= lookback)
        pullback_ok = near_vwap and confirm_side(direction)

        # Reclaim path
        reclaim_ok = False
        if direction == "LONG":
            if low_tag_ts is not None and (now - low_tag_ts) <= timedelta(minutes=30):
                reclaim_ok = confirm_side("LONG")
        else:
            if high_tag_ts is not None and (now - high_tag_ts) <= timedelta(minutes=30):
                reclaim_ok = confirm_side("SHORT")

        if is_opening_mode:
            if not reclaim_ok:
                blocks["opening_require_breakout"] += 1
                continue
            if len(m_close) < 4:
                blocks["opening_direction_not_ready"] += 1
                continue
            close_1m_now = float(m_close[-1])
            close_1m_prev = float(m_close[-2])
            close_1m_3bars_ago = float(m_close[-4])
            dir1m = 0
            ref1m = float(vwap)
            if close_1m_now > ref1m:
                dir1m = 1
            elif close_1m_now < ref1m:
                dir1m = -1
            elif close_1m_now > close_1m_prev:
                dir1m = 1
            elif close_1m_now < close_1m_prev:
                dir1m = -1
            dir3m = 0
            if close_1m_now > close_1m_3bars_ago:
                dir3m = 1
            elif close_1m_now < close_1m_3bars_ago:
                dir3m = -1
            if direction == "LONG" and not (dir1m > 0 and dir3m > 0):
                blocks["opening_direction_hard_gate"] += 1
                continue
            if direction == "SHORT" and not (dir1m < 0 and dir3m < 0):
                blocks["opening_direction_hard_gate"] += 1
                continue
        elif (not pullback_ok) and (not reclaim_ok):
            blocks["entry_not_confirmed"] += 1
            continue

        # Wrong-side veto after extreme
        if direction == "SHORT" and low_tag_ts is not None and (now - low_tag_ts) <= timedelta(minutes=30):
            if not confirm_side("SHORT"):
                blocks["wrong_side_veto_after_new_low"] += 1
                continue
        if direction == "LONG" and high_tag_ts is not None and (now - high_tag_ts) <= timedelta(minutes=30):
            if not confirm_side("LONG"):
                blocks["wrong_side_veto_after_new_high"] += 1
                continue

        trade_age_limit = qqq_trade_allow_age_sec_tier1 if tier == "T1" else qqq_trade_allow_age_sec_tier2
        if age_sec > trade_age_limit:
            blocks["gate0_calc_only"] += 1
            continue

        return EntryResult(
            day=day,
            data_source=data_source,
            traded=True,
            tier=tier,
            path="RECLAIM" if (is_opening_mode or reclaim_ok) else "PULLBACK",
            direction=direction,
            entry_ts=now.isoformat(),
            entry_px_underlying=round(px_now, 4),
            entry_dev_atr=round(dev / max(atr_cap, 1e-9), 4),
            blocks_top=blocks.most_common(10),
        )

    return EntryResult(
        day=day,
        data_source=data_source,
        traded=False,
        tier="",
        path="",
        direction="",
        entry_ts="",
        entry_px_underlying=0.0,
        entry_dev_atr=0.0,
        blocks_top=blocks.most_common(10) if blocks else [("no_entry", 1)],
    )


def _atr_1m_local(closes: list[float], highs: list[float], lows: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: list[float] = []
    for i in range(len(closes) - period, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(float(tr))
    return float(sum(trs) / len(trs)) if trs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", action="append", required=True)
    ap.add_argument("--sfs-n", type=int, default=120000)
    ap.add_argument("--logic-retreat-min-hold-sec", type=float, default=None)
    ap.add_argument("--entry-cooloff-end", default="09:30:00")
    ap.add_argument("--opening-mode-end", default="09:40:00")
    ap.add_argument("--opening-breakout-vol-min", type=float, default=1.2)
    ap.add_argument("--out-entry", default="runtime/state/qqq_aonly_tiered_entry.csv")
    ap.add_argument("--out-daily", default="runtime/state/qqq_aonly_tiered_daily.csv")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path("config.yml").read_text(encoding="utf-8"))
    extras = {}
    for s in cfg.get("strategies", []):
        if s.get("id") == "qqq_intraday_001":
            extras = ((s.get("params") or {}).get("extras") or {})
            break
    if args.logic_retreat_min_hold_sec is not None:
        extras = dict(extras)
        extras["logic_retreat_min_hold_sec"] = float(args.logic_retreat_min_hold_sec)

    entries: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []

    for day in args.day:
        ticks, data_source = asyncio.run(_load_ticks(day, int(args.sfs_n)))
        atr_seed_tqqq = _load_atr_seed(Path("runtime/state/qqq_intraday_atr_seed.json"), day).get("TQQQ", 0.0)
        entry = run_day_aonly_tiered(
            day=day,
            ticks=ticks,
            data_source=data_source,
            atr_seed_tqqq=float(atr_seed_tqqq),
            a_vol_min_base=0.0,
            a_pullback_mult_open=1.4,
            a_pullback_mult_late=1.5,
            a_pullback_switch_time=time.fromisoformat("11:00:00"),
            or_throttle_enable=False,
            early_lock_enable=False,
            entry_cooloff_end=time.fromisoformat(str(args.entry_cooloff_end)),
            opening_mode_end=time.fromisoformat(str(args.opening_mode_end)),
            opening_breakout_vol_min=float(args.opening_breakout_vol_min),
        )
        entries.append(
            {
                "day": entry.day,
                "data_source": entry.data_source,
                "traded": int(entry.traded),
                "tier": entry.tier,
                "path": entry.path,
                "direction": entry.direction,
                "entry_ts": entry.entry_ts,
                "entry_px_underlying": entry.entry_px_underlying,
                "entry_dev_atr": entry.entry_dev_atr,
                "top_blocked": json.dumps(entry.blocks_top, ensure_ascii=False),
            }
        )

        if not entry.traded:
            reason = entry.blocks_top[0][0] if entry.blocks_top else "no_entry"
            daily.append(
                {
                    "day": day,
                    "data_source": entry.data_source,
                    "traded": 0,
                    "tier": "",
                    "path": "",
                    "direction": "",
                    "entry_ts": "",
                    "entry_px_underlying": "",
                    "exit_ts": "",
                    "exit_reason": "",
                    "pnl_usd": "",
                    "top_no_trade_reason": reason,
                }
            )
            continue

        bars, src2 = _load_day_exec_bars(day)
        exit_res = _exit_for_entry(
            day=day,
            direction=entry.direction,
            entry_ts=datetime.fromisoformat(entry.entry_ts),
            extras=extras,
            bars=bars,
        )
        daily.append(
            {
                "day": day,
                "data_source": src2,
                "traded": 1,
                "tier": entry.tier,
                "path": entry.path,
                "direction": entry.direction,
                "entry_ts": entry.entry_ts,
                "entry_px_underlying": entry.entry_px_underlying,
                "exit_ts": exit_res.get("exit_ts", ""),
                "exit_reason": exit_res.get("exit_reason", exit_res.get("error", "unknown")),
                "pnl_usd": exit_res.get("pnl_usd", ""),
                "top_no_trade_reason": "",
            }
        )

    out_entry = Path(args.out_entry)
    out_entry.parent.mkdir(parents=True, exist_ok=True)
    with out_entry.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(entries[0].keys()))
        w.writeheader()
        w.writerows(entries)

    out_daily = Path(args.out_daily)
    with out_daily.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(daily[0].keys()))
        w.writeheader()
        w.writerows(daily)

    print(out_entry)
    print(out_daily)
    for r in daily:
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
