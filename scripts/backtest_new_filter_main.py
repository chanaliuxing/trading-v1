#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_new_filter_main.py
===========================
v47_fast_drift aligned backtest.

Key alignment points:
- Entry model: FAST/DRIFT with sign_ok + ATR-effective + cooldown + veto.
- VWAP cross state machine: ABOVE/MIDDLE/BELOW with band.
- FAST cooldown: arm when cross_count makes new high above threshold.
- Exit: target(ATR-locked) + logic_retreat(hold+wrong side VWAP) + EOD.
- 回测口径固定：openclose_base = QQQ 09:30 1m open.
- 实盘口径说明：openclose_base = price_base(09:30:00-09:30:10, 1s VWAP-like).
- logic_retreat 口径：判定使用 QQQ 1m close vs QQQ VWAP；执行使用 TQQQ/SQQQ 平仓价。

Notes:
- Historical backtest cannot fully reproduce live guards like open-order checks,
  account position sync, and real-time freshness checks.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Optional
import sys

from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from libs.db.ch_pool import get_client

ET = ZoneInfo("America/New_York")


@dataclass
class Tick:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    entry_time: datetime
    entry_px: float
    entry_mode: str
    entry_direction: str
    exec_symbol: str
    qty: int
    notional: float
    atr_eff_at_entry: float
    atr_entry_used: float
    vwap_crosses: int

    exit_time: Optional[datetime] = None
    exit_px: Optional[float] = None
    exit_reason: Optional[str] = None
    exec_pnl: Optional[float] = None
    minutes_held: Optional[int] = None
    target_px: Optional[float] = None
    target_pnl_if_hit: Optional[float] = None
    mfe_5m: float = 0.0
    mae_5m: float = 0.0
    fast_reversal_guard_hit: bool = False
    is_first_trade: bool = False
    day_realized_pnl_before: float = 0.0
    chop_lock_on_entry: bool = False


@dataclass
class DayOutcome:
    day: str
    trade: Optional[Trade]
    no_trade_reason: str
    reject_counts: dict[str, int]


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_hhmm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def load_1m_bars_from_clickhouse(day: str, symbol: str) -> list[Tick]:
    client = get_client()
    rows = client.query(
        """
        SELECT ts_start, open, high, low, close, volume
        FROM bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND d = toDate(%(day)s)
        ORDER BY ts_start ASC
        """,
        {"day": day, "symbol": symbol},
    )

    ticks: list[Tick] = []
    for row in rows:
        ts_raw = row.get("ts_start")
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        else:
            ts = ts.astimezone(ET)

        close_px = float(row.get("close", 0.0))
        if close_px <= 0:
            continue

        ticks.append(
            Tick(
                ts=ts,
                open=float(row.get("open", 0.0)),
                high=float(row.get("high", 0.0)),
                low=float(row.get("low", 0.0)),
                close=close_px,
                volume=float(row.get("volume", 0.0)),
            )
        )
    return ticks


def calculate_atr(ticks: list[Tick], idx: int, period: int = 14) -> Optional[float]:
    if idx <= 0 or idx < period:
        return None

    trs: list[float] = []
    start = idx - period + 1
    for i in range(start, idx + 1):
        tr = max(
            ticks[i].high - ticks[i].low,
            abs(ticks[i].high - ticks[i - 1].close),
            abs(ticks[i].low - ticks[i - 1].close),
        )
        trs.append(tr)
    return (sum(trs) / len(trs)) if trs else None


def calculate_vwap(ticks: list[Tick], end_idx: int) -> float:
    if end_idx < 0:
        return 0.0

    sum_pv = 0.0
    sum_v = 0.0
    for i in range(0, end_idx + 1):
        t = ticks[i]
        tp = (t.high + t.low + t.close) / 3.0
        sum_pv += tp * t.volume
        sum_v += t.volume
    return (sum_pv / sum_v) if sum_v > 0 else 0.0


def state_from_vwap(px: float, vwap: float, band_bps: float = 8.0) -> str:
    if vwap <= 0:
        return "MIDDLE"
    upper = vwap * (1.0 + band_bps / 10000.0)
    lower = vwap * (1.0 - band_bps / 10000.0)
    if px > upper:
        return "ABOVE"
    if px < lower:
        return "BELOW"
    return "MIDDLE"


def minute_close_at_or_before(ticks_by_ts: dict[datetime, Tick], target_ts: datetime) -> Optional[float]:
    m = target_ts.replace(second=0, microsecond=0)
    t = ticks_by_ts.get(m)
    return float(t.close) if t else None


def _rth_ticks(ticks: list[Tick]) -> list[Tick]:
    return [t for t in ticks if time(9, 30) <= t.ts.time() <= time(16, 0)]


def load_atr_seed_map(seed_path: str) -> dict:
    p = Path(seed_path).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def atr_seed_for_day(seed_map: dict, day: str, symbol: str) -> Optional[float]:
    days = seed_map.get("days", {}) if isinstance(seed_map, dict) else {}
    if isinstance(days, dict):
        d = days.get(day)
        if isinstance(d, dict):
            v = d.get(symbol)
            if v is not None:
                try:
                    vv = float(v)
                    if vv > 0:
                        return vv
                except Exception:
                    pass

        # Backfill fallback: use nearest prior day that has a valid seed.
        try:
            target = datetime.strptime(day, "%Y-%m-%d").date()
            candidates: list[tuple[date, float]] = []
            for k, rec in days.items():
                if not isinstance(rec, dict):
                    continue
                sv = rec.get(symbol)
                if sv is None:
                    continue
                try:
                    kd = datetime.strptime(str(k), "%Y-%m-%d").date()
                    fv = float(sv)
                    if fv > 0 and kd <= target:
                        candidates.append((kd, fv))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1]
        except Exception:
            pass

    latest = seed_map.get("latest", {}) if isinstance(seed_map, dict) else {}
    if isinstance(latest, dict):
        atr_pre = latest.get("atr_pre")
        if isinstance(atr_pre, dict):
            v = atr_pre.get(symbol)
            if v is not None:
                try:
                    vv = float(v)
                    if vv > 0:
                        return vv
                except Exception:
                    pass
    return None


def effective_atr_backtest(
    symbol: str,
    symbol_ticks: list[Tick],
    idx: int,
    atr_seed: Optional[float],
    atr_cap_mult: float,
    now: datetime,
    period: int = 14,
) -> Optional[float]:
    """
    Mirror live _effective_atr priority with available historical granularity:
    1) Live ATR from execution symbol (1m proxy in backtest)
    2) Pre-seed ATR from persisted seed file
    3) Emergency fallback after 09:45
    """
    atr_live = calculate_atr(symbol_ticks, idx, period=period)
    if atr_live is not None and atr_live > 0:
        if atr_seed is not None and atr_seed > 0:
            return float(min(float(atr_live), float(atr_cap_mult * atr_seed)))
        return float(atr_live)

    if atr_seed is not None and atr_seed > 0:
        return float(atr_seed)

    if now.time() >= time(9, 45):
        return 0.35 if symbol == "TQQQ" else 0.30

    return None


def load_daily_atr_context(start_day: str, end_day: str) -> tuple[dict[str, float], dict[str, float]]:
    """
    Build daily ATR context from 1m QQQ bars.
    Returns:
    - atr_d_by_day: ATR_d for each day (ATR14_daily of t-1)
    - atr_rank_by_day: rank(ATR_d) over trailing 30 ATR14 values (0..1)
    """
    start = _parse_date(start_day) - timedelta(days=120)
    end = _parse_date(end_day)
    client = get_client()
    rows = client.query(
        """
        SELECT
          d,
          argMin(open, ts_start) AS o,
          max(high) AS h,
          min(low) AS l,
          argMax(close, ts_start) AS c
        FROM bars_1m_ibkr
        WHERE symbol = 'QQQ'
          AND d >= toDate(%(start)s)
          AND d <= toDate(%(end)s)
        GROUP BY d
        ORDER BY d ASC
        """,
        {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
    )

    ds: list[str] = []
    hs: list[float] = []
    ls: list[float] = []
    cs: list[float] = []
    for r in rows:
        d_raw = r.get("d")
        if d_raw is None:
            continue
        d_str = d_raw.strftime("%Y-%m-%d") if hasattr(d_raw, "strftime") else str(d_raw)
        ds.append(d_str)
        hs.append(float(r.get("h", 0.0) or 0.0))
        ls.append(float(r.get("l", 0.0) or 0.0))
        cs.append(float(r.get("c", 0.0) or 0.0))

    trs: list[float] = []
    atr14: list[Optional[float]] = []
    for i in range(len(ds)):
        prev_close = cs[i - 1] if i > 0 else cs[i]
        tr = max(hs[i] - ls[i], abs(hs[i] - prev_close), abs(ls[i] - prev_close))
        trs.append(float(tr))
        if i >= 13:
            atr14.append(sum(trs[i - 13:i + 1]) / 14.0)
        else:
            atr14.append(None)

    atr_d_by_day: dict[str, float] = {}
    atr_rank_by_day: dict[str, float] = {}
    for i, d in enumerate(ds):
        if i <= 0:
            continue
        atr_d = atr14[i - 1]
        if atr_d is None or atr_d <= 0:
            continue
        atr_d_by_day[d] = float(atr_d)
        win: list[float] = []
        j0 = max(0, i - 30)
        for j in range(j0, i):
            v = atr14[j]
            if v is not None and v > 0:
                win.append(float(v))
        if win:
            leq = sum(1 for v in win if v <= float(atr_d))
            atr_rank_by_day[d] = float(leq / len(win))
        else:
            atr_rank_by_day[d] = 0.5
    return atr_d_by_day, atr_rank_by_day


def _quantile_linear(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    qq = min(1.0, max(0.0, float(q)))
    if len(xs) == 1:
        return xs[0]
    pos = qq * (len(xs) - 1)
    lo = int(pos)
    hi = min(len(xs) - 1, lo + 1)
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return (1.0 - w) * xs[lo] + w * xs[hi]


def _opening_vol_surge_0938(day: str) -> Optional[float]:
    qqq = _rth_ticks(load_1m_bars_from_clickhouse(day, "QQQ"))
    if not qqq:
        return None
    by_t = {t.ts.time(): t for t in qqq}
    t0938 = by_t.get(time(9, 38))
    if t0938 is None:
        return None
    base: list[float] = []
    for mm in range(30, 38):
        tk = by_t.get(time(9, mm))
        if tk is None:
            return None
        base.append(float(tk.volume))
    avg_base = (sum(base) / len(base)) if base else 0.0
    if avg_base <= 0:
        return None
    return float(float(t0938.volume) / avg_base)


def simulate_day(
    day: str,
    notional_usd: float,
    target_atr_mult: float,
    tp2_trend_atr_mult: float,
    hard_stop_atr_mult: float,
    stop_min_abs: float,
    logic_retreat_minutes: int,
    logic_retreat_buffer_atr: float,
    atr_cap_mult: float,
    momentum_deadzone: float,
    fast_v3m_atr_min: float,
    fast_vol_min: float,
    drift_bar_min_atr: float,
    drift_cum_atr_min: float,
    fast_crosses_threshold: int,
    fast_cooldown_min: float,
    veto_rebound_live_atr: float,
    atr_seed_map: dict,
    atr_d: Optional[float],
    atr_rank: float,
    atr_rank_chop_max: float,
    early_breakout_move_atr: float,
    early_breakout_vol_min: float,
    chop_mode: str,
    mr_tp_atr: float,
    mr_sl_atr: float,
    mr_max_hold_min: int,
    a_day_size_mult: float,
    a_day_atr_rank_min: float,
    drift_cum_atrd_min: float,
    early_lock_mode: str,
    delayed_confirm_move5m_min: float,
    delayed_confirm_end: time,
    delayed_confirm_adaptive_enable: bool = False,
    delayed_confirm_adaptive_move5m_min: float = 0.08,
    delayed_confirm_adaptive_cross_count_max: int = 6,
    delayed_confirm_adaptive_vwap_dev_atr_d_min: float = 0.12,
    min_entry_time: Optional[datetime] = None,
    day_realized_pnl_before: float = 0.0,
    entry_confirm_delay_min: int = 0,
    long_drift_second_confirm: bool = False,
    long_drift_second_confirm_side: str = "long",
    long_drift_second_confirm_window_start: time = time(9, 45),
    long_drift_second_confirm_window_end: time = time(10, 29),
    long_drift_second_confirm_delay_min: int = 1,
    breakout_followthrough_confirm: bool = False,
    skip_short_on_narrow_day: bool = False,
    narrow_open_range_atr_max: float = 0.0,
    first_trade_block_0945_0950: bool = False,
    first_trade_second_test_require_breakout: bool = False,
    first_trade_skip_failed_breakout_pullback: bool = False,
    entry_dist_0940_max_atr_d: Optional[float] = None,
    fast_reversal_guard: str = "none",
    fast_reversal_v3_only_first_trade: bool = False,
    fast_reversal_v3_window_start: time = time(9, 45),
    fast_reversal_v3_window_end: time = time(10, 5),
    fast_reversal_v3_max_entry_dist_atr_d: float = 0.05,
    early_override_direct_entry: bool = False,
    early_override_window_start: time = time(9, 39),
    early_override_window_end: time = time(9, 45),
    early_override_max_counter_bars: int = 0,
    early_override_last3_confirm: bool = False,
    early_override_min_vwap_dist_atr_d: float = 0.0,
    early_override_max_cross_count: int = 999,
    early_override_disable_add: bool = False,
    early_quick_exit_min: Optional[int] = None,
    early_continuation_min_atr_d: float = 0.0,
    low_atr_release_enable: bool = False,
    low_atr_release_window_start: time = time(10, 0),
    low_atr_release_window_end: time = time(11, 30),
    low_atr_release_max_cross_count: int = 1,
    low_atr_release_min_vwap_dist_atr_d: float = 0.18,
    low_atr_release_drift_bar_min_atr: float = 0.10,
    low_atr_release_drift_cum_atr_min: float = 0.55,
    low_atr_release_quick_exit_min: int = 2,
    low_atr_release_continuation_min_atr_d: float = 0.15,
    chop_lock_enable: bool = False,
    chop_lock_window_start: time = time(9, 45),
    chop_lock_window_end: time = time(14, 0),
    chop_lock_cross_count_15m_min: int = 3,
    chop_lock_require_day_pnl_nonpositive: bool = True,
    chop_lock_disable_add: bool = False,
    chop_lock_probe_ratio: float = 0.70,
    chop_lock_rearm_vwap_band_atr_d: float = 0.0,
    anti_choptop_enable: bool = False,
    anti_choptop_apply_to: str = "drift",
    anti_choptop_window_start: time = time(9, 45),
    anti_choptop_window_end: time = time(12, 30),
    anti_choptop_rank_high: float = 0.93,
    anti_choptop_rank_low: float = 0.07,
    anti_choptop_sign_flip_5m_min: int = 2,
    anti_choptop_dev_atr_d_min: float = 1.10,
    anti_choptop_ttl_min: int = 3,
    anti_chase_chop_enable: bool = False,
    anti_chase_window_start: time = time(9, 45),
    anti_chase_window_end: time = time(14, 0),
    anti_chase_apply_to: str = "all",
    anti_chase_rank15_min: float = 0.93,
    anti_chase_dist_vwap_atr_d_min: float = 0.12,
    anti_chase_fast_rank15_min: Optional[float] = None,
    anti_chase_fast_dist_vwap_atr_d_min: Optional[float] = None,
    anti_chase_drift_rank15_min: Optional[float] = None,
    anti_chase_drift_dist_vwap_atr_d_min: Optional[float] = None,
    anti_chase_signal_ttl_sec: int = 60,
    chop_cross_count_min: int = 4,
    chop_vwap_dist_atr_d_max: float = 0.8,
    chop_drift_5m_atr_d_max: float = 0.9,
    chop_flip_5m_min: int = 3,
    stop_reentry_cooldown_min: float = 0.0,
    last_stop_exit_at: Optional[datetime] = None,
    last_stop_exit_side: Optional[str] = None,
    day_hard_stop_count_before: int = 0,
    day_stop_after_hard_stops: Optional[int] = None,
    day_stop_after_hard_stops_chop_only: bool = False,
    trade_seq: int = 1,
    first_trade_was_hard_stop: bool = False,
    pre_second_chop_guard_enable: bool = False,
    pre_second_chop_window_start: time = time(9, 45),
    pre_second_chop_window_end: time = time(14, 0),
    pre_second_chop_cross_count_15m_min: int = 3,
    pre_second_chop_require_first_stop: bool = True,
    pre_second_chop_action: str = "degrade",
    pre_second_chop_probe_ratio: float = 0.50,
) -> DayOutcome:
    qqq = _rth_ticks(load_1m_bars_from_clickhouse(day, "QQQ"))
    tqqq = _rth_ticks(load_1m_bars_from_clickhouse(day, "TQQQ"))
    sqqq = _rth_ticks(load_1m_bars_from_clickhouse(day, "SQQQ"))

    reject_counts: defaultdict[str, int] = defaultdict(int)

    if not qqq:
        return DayOutcome(day=day, trade=None, no_trade_reason="missing_qqq_data", reject_counts={})
    if not tqqq:
        return DayOutcome(day=day, trade=None, no_trade_reason="missing_tqqq_data", reject_counts={})
    if not sqqq:
        return DayOutcome(day=day, trade=None, no_trade_reason="missing_sqqq_data", reject_counts={})

    q_by_ts = {t.ts.replace(second=0, microsecond=0): t for t in qqq}
    t_by_ts = {t.ts.replace(second=0, microsecond=0): t for t in tqqq}
    s_by_ts = {t.ts.replace(second=0, microsecond=0): t for t in sqqq}
    idx_by_ts = {t.ts.replace(second=0, microsecond=0): i for i, t in enumerate(qqq)}

    if atr_d is None or atr_d <= 0:
        return DayOutcome(day=day, trade=None, no_trade_reason="atr_d_unavailable", reject_counts={})

    # Day-level filter: skip low opening expansion days.
    # If 09:30-09:45 range < 0.4 * QQQ ATR(14), do not trade this day.
    opening_ticks = [t for t in qqq if time(9, 30) <= t.ts.time() <= time(9, 45)]
    opening_range_ratio: Optional[float] = None
    if len(opening_ticks) >= 2:
        opening_range = max(t.high for t in opening_ticks) - min(t.low for t in opening_ticks)
        open_ref_idx = next((k for k, t in enumerate(qqq) if t.ts.time() >= time(9, 45)), None)
        atr_open_ref = calculate_atr(qqq, open_ref_idx, period=14) if open_ref_idx is not None else None
        if atr_open_ref is not None and atr_open_ref > 0 and opening_range < (0.4 * float(atr_open_ref)):
            return DayOutcome(day=day, trade=None, no_trade_reason="opening_range_lt_0.4atr", reject_counts={})
        if atr_open_ref is not None and atr_open_ref > 0:
            opening_range_ratio = float(opening_range / float(atr_open_ref))

    narrow_day_flag = (
        bool(skip_short_on_narrow_day)
        and opening_range_ratio is not None
        and opening_range_ratio <= float(narrow_open_range_atr_max)
    )

    cross_state = "MIDDLE"
    cross_count = 0
    fast_cooldown_until: Optional[datetime] = None
    fast_cooldown_trigger_cross = 0

    def _rank_15m(idx: int) -> Optional[float]:
        start = max(0, idx - 15)
        if idx <= start:
            return None
        hist = [float(qqq[k].close) for k in range(start, idx)]
        if not hist:
            return None
        now_px = float(qqq[idx].close)
        le = sum(1 for x in hist if x <= now_px)
        return float(le) / float(len(hist))

    def _cross_count_15m(idx: int) -> int:
        if idx < 1:
            return 0
        start = max(0, idx - 15)
        prev = 0
        count = 0
        for k in range(start, idx + 1):
            vk = calculate_vwap(qqq, k)
            if vk <= 0:
                continue
            d = float(qqq[k].close) - float(vk)
            s = 1 if d > 0 else (-1 if d < 0 else 0)
            if s == 0:
                continue
            if prev != 0 and s != prev:
                count += 1
            prev = s
        return count

    def _sign_flip_5m(idx: int) -> int:
        if idx < 2:
            return 0
        step_start = max(1, idx - 4)
        step_signs: list[int] = []
        for k in range(step_start, idx + 1):
            d = float(qqq[k].close) - float(qqq[k - 1].close)
            step_signs.append(1 if d > 0 else (-1 if d < 0 else 0))
        flips = 0
        prev = step_signs[0] if step_signs else 0
        for s in step_signs[1:]:
            if s == 0:
                continue
            if prev == 0:
                prev = s
                continue
            if s != prev:
                flips += 1
            prev = s
        return flips

    def _is_chop_realtime(idx: int, vwap_now_val: float) -> bool:
        if idx < 5 or atr_d <= 0:
            return False
        start = max(0, idx - 15)
        signs: list[int] = []
        for k in range(start, idx + 1):
            vk = calculate_vwap(qqq, k)
            if vk <= 0:
                signs.append(0)
                continue
            d = float(qqq[k].close) - float(vk)
            signs.append(1 if d > 0 else (-1 if d < 0 else 0))

        cross_15m = 0
        prev = signs[0] if signs else 0
        for s in signs[1:]:
            if s == 0:
                continue
            if prev == 0:
                prev = s
                continue
            if s != prev:
                cross_15m += 1
            prev = s

        drift_5m_atr_d = abs(float(qqq[idx].close) - float(qqq[idx - 5].close)) / float(atr_d)
        vwap_dist_atr_d = abs(float(qqq[idx].close) - float(vwap_now_val)) / float(atr_d)

        step_signs: list[int] = []
        step_start = max(1, idx - 4)
        for k in range(step_start, idx + 1):
            d = float(qqq[k].close) - float(qqq[k - 1].close)
            step_signs.append(1 if d > 0 else (-1 if d < 0 else 0))
        flip_5m = 0
        prev_step = step_signs[0] if step_signs else 0
        for s in step_signs[1:]:
            if s == 0:
                continue
            if prev_step == 0:
                prev_step = s
                continue
            if s != prev_step:
                flip_5m += 1
            prev_step = s

        return (
            (cross_15m >= int(chop_cross_count_min))
            or (
                vwap_dist_atr_d <= float(chop_vwap_dist_atr_d_max)
                and drift_5m_atr_d <= float(chop_drift_5m_atr_d_max)
            )
            or (flip_5m >= int(chop_flip_5m_min))
        )

    market_open_idx = 0
    entry_window_start = time(9, 45)
    entry_window_end = time(14, 0)
    anti_choptop_block_until: dict[str, Optional[datetime]] = {"LONG": None, "SHORT": None}
    day_open_tick = next((t for t in qqq if t.ts.time() >= time(9, 30)), None)
    day_open_px = float(day_open_tick.open) if day_open_tick is not None else None

    # Unified early-session structure window: 09:30-09:40
    window_10m = [t for t in qqq if time(9, 30) <= t.ts.time() <= time(9, 40)]
    close_0940_tick = next((t for t in qqq if t.ts.time() == time(9, 40)), None)
    close_0940_px = float(close_0940_tick.close) if close_0940_tick is not None else (float(window_10m[-1].close) if window_10m else None)
    move_ratio = 0.0
    range_ratio = 0.0
    early_dir_lock: Optional[str] = None
    breakout_hi_0940: Optional[float] = None
    breakout_lo_0940: Optional[float] = None
    trend_ready = False
    if day_open_px is not None and close_0940_px is not None and atr_d > 0 and window_10m:
        move_10m = abs(float(close_0940_px) - float(day_open_px))
        range_10m = max(float(t.high) for t in window_10m) - min(float(t.low) for t in window_10m)
        move_ratio = float(move_10m / float(atr_d))
        range_ratio = float(range_10m / float(atr_d))
        breakout_hi_0940 = max(float(t.high) for t in window_10m)
        breakout_lo_0940 = min(float(t.low) for t in window_10m)

        # Structure-B: 09:36-09:40 contains 3 same-direction pushes via HH+HL / LL+LH.
        bars_3640 = [t for t in qqq if time(9, 36) <= t.ts.time() <= time(9, 40)]
        push_up = 0
        push_dn = 0
        if len(bars_3640) >= 4:
            for j in range(1, len(bars_3640)):
                prev = bars_3640[j - 1]
                cur = bars_3640[j]
                if float(cur.high) > float(prev.high) and float(cur.low) > float(prev.low):
                    push_up += 1
                if float(cur.low) < float(prev.low) and float(cur.high) < float(prev.high):
                    push_dn += 1
        structure_b = (push_up >= 3) or (push_dn >= 3)
        trend_ready = (move_ratio >= 0.20) or structure_b
        if float(close_0940_px) > float(day_open_px):
            early_dir_lock = "LONG"
        elif float(close_0940_px) < float(day_open_px):
            early_dir_lock = "SHORT"

    delayed_confirmed = False
    delayed_dir_lock: Optional[str] = None

    # Opening cross-count (09:30-09:45) used as an anti-chop filter for early override.
    opening_cross_state = "MIDDLE"
    opening_cross_count = 0
    for ii, tk in enumerate(qqq):
        tt = tk.ts.time()
        if tt < time(9, 30) or tt > time(9, 45):
            continue
        vwk = calculate_vwap(qqq, ii)
        st = state_from_vwap(float(tk.close), float(vwk), band_bps=8.0)
        if (opening_cross_state == "ABOVE" and st == "BELOW") or (opening_cross_state == "BELOW" and st == "ABOVE"):
            opening_cross_count += 1
        if st != "MIDDLE":
            opening_cross_state = st

    # Regime selection: early trend override, else ATR-rank split.
    early_override = False
    early_override_direction: Optional[str] = None
    tick_0938 = next((t for t in qqq if t.ts.time() == time(9, 38)), None)
    if day_open_px is not None and tick_0938 is not None and atr_d > 0:
        i_0938 = idx_by_ts.get(tick_0938.ts.replace(second=0, microsecond=0))
        if i_0938 is not None and i_0938 >= 1:
            vwap_0938 = calculate_vwap(qqq, i_0938)
            early_move = abs(float(tick_0938.close) - float(day_open_px))
            side_ref = 1 if float(tick_0938.close) > float(vwap_0938) else (-1 if float(tick_0938.close) < float(vwap_0938) else 0)
            same_side = True
            side_count = 0
            counter_bars = 0
            last3_sides: list[int] = []
            for k in range(0, i_0938 + 1):
                tk = qqq[k]
                if tk.ts.time() < time(9, 30) or tk.ts.time() > time(9, 38):
                    continue
                vwk = calculate_vwap(qqq, k)
                diff = float(tk.close) - float(vwk)
                side = 1 if diff > 0 else (-1 if diff < 0 else 0)
                if side_ref == 0 or side == 0:
                    same_side = False
                    break
                side_count += 1
                last3_sides.append(side)
                if side != side_ref:
                    counter_bars += 1
            if side_count <= 0:
                same_side = False
            else:
                same_side = counter_bars <= max(0, int(early_override_max_counter_bars))
            last3_ok = True
            if bool(early_override_last3_confirm):
                if len(last3_sides) < 3:
                    last3_ok = False
                else:
                    last3_ok = all(s == side_ref for s in last3_sides[-3:])
            vwap_dist_ok = True
            if float(early_override_min_vwap_dist_atr_d) > 0 and atr_d > 0:
                vwap_dist_ok = (abs(float(tick_0938.close) - float(vwap_0938)) / float(atr_d)) >= float(early_override_min_vwap_dist_atr_d)
            prev_vol = [qqq[k].volume for k in range(max(0, i_0938 - 8), i_0938)]
            vol_surge = (float(tick_0938.volume) / max(1e-9, (sum(prev_vol) / len(prev_vol)))) if prev_vol else 0.0
            early_override = (
                early_move >= (float(early_breakout_move_atr) * float(atr_d))
                and same_side
                and last3_ok
                and vwap_dist_ok
                and vol_surge >= float(early_breakout_vol_min)
                and opening_cross_count <= max(0, int(early_override_max_cross_count))
            )
            if early_override:
                if side_ref > 0:
                    early_override_direction = "LONG"
                elif side_ref < 0:
                    early_override_direction = "SHORT"

    regime = "TREND"
    low_atr_release_active = False
    # Hybrid priority:
    # - If atr_rank is below threshold, day is normally skipped.
    # - But early_override can still open a limited opening-window path.
    if atr_rank <= float(atr_rank_chop_max):
        if early_override:
            regime = "CHOP_EARLY_OVERRIDE"
            entry_window_start = early_override_window_start
            entry_window_end = early_override_window_end
            trend_ready = True
        elif bool(low_atr_release_enable) and str(chop_mode).lower() == "skip":
            regime = "CHOP_RELEASE"
            low_atr_release_active = True
            entry_window_start = low_atr_release_window_start
            entry_window_end = low_atr_release_window_end
        else:
            regime = "CHOP"
    else:
        regime = "TREND"
        if early_override:
            entry_window_start = time(9, 30)
            trend_ready = True

    # Position sizing tiers:
    # - CHOP: handled by chop_mode (skip or MR)
    # - TREND A-day: scale up size
    # - TREND normal: base size
    trend_size_mult = float(a_day_size_mult) if (regime == "TREND" and (early_override or atr_rank >= float(a_day_atr_rank_min))) else 1.0

    if regime == "CHOP":
        if str(chop_mode).lower() == "skip":
            return DayOutcome(day=day, trade=None, no_trade_reason="regime_chop_skip", reject_counts={})
        chop_start = time(9, 45)
        chop_end = time(11, 30)
        for i in range(market_open_idx, len(qqq)):
            now_tick = qqq[i]
            now = now_tick.ts.replace(second=0, microsecond=0)
            if min_entry_time is not None and now < min_entry_time:
                continue
            if not (chop_start <= now.time() <= chop_end):
                continue
            vwap_now = calculate_vwap(qqq, i)
            if vwap_now <= 0:
                continue
            dev = (float(now_tick.close) - float(vwap_now)) / float(atr_d)
            if i < 2:
                continue
            dev1 = (float(qqq[i - 1].close) - float(calculate_vwap(qqq, i - 1))) / float(atr_d)
            dev2 = (float(qqq[i - 2].close) - float(calculate_vwap(qqq, i - 2))) / float(atr_d)

            direction: Optional[str] = None
            exec_symbol: Optional[str] = None
            exec_map: Optional[dict[datetime, Tick]] = None
            exec_ticks: Optional[list[Tick]] = None
            if dev >= 0.6 and dev2 > dev1 > dev:
                direction = "SHORT"
                exec_symbol = "SQQQ"
                exec_map = s_by_ts
                exec_ticks = sqqq
            elif dev <= -0.6 and dev2 < dev1 < dev:
                direction = "LONG"
                exec_symbol = "TQQQ"
                exec_map = t_by_ts
                exec_ticks = tqqq
            else:
                continue

            ex_now = exec_map.get(now)
            if ex_now is None:
                continue
            qty = int((notional_usd * 1.0) / float(ex_now.close))
            if qty <= 0:
                continue

            trade = Trade(
                entry_time=now,
                entry_px=float(ex_now.close),
                entry_mode="MR",
                entry_direction=direction,
                exec_symbol=exec_symbol,
                qty=qty,
                notional=qty * float(ex_now.close),
                atr_eff_at_entry=float(atr_d),
                atr_entry_used=float(atr_d),
                vwap_crosses=0,
            )
            trade.target_pnl_if_hit = round(float(mr_tp_atr) * float(atr_d) * qty, 2)

            qqq_entry = float(now_tick.close)
            tp_q = qqq_entry - (float(mr_tp_atr) * float(atr_d)) if direction == "SHORT" else qqq_entry + (float(mr_tp_atr) * float(atr_d))
            sl_q = qqq_entry + (float(mr_sl_atr) * float(atr_d)) if direction == "SHORT" else qqq_entry - (float(mr_sl_atr) * float(atr_d))
            ex_idx = next((k for k, t in enumerate(exec_ticks) if t.ts.replace(second=0, microsecond=0) == now), None)
            if ex_idx is None:
                continue

            for j in range(ex_idx + 1, len(exec_ticks)):
                ex = exec_ticks[j]
                ts = ex.ts.replace(second=0, microsecond=0)
                q_tick = q_by_ts.get(ts)
                if q_tick is None:
                    continue
                held = int((ts - trade.entry_time).total_seconds() // 60)
                q_now = float(q_tick.close)
                hit_tp = (q_now <= tp_q) if direction == "SHORT" else (q_now >= tp_q)
                hit_sl = (q_now >= sl_q) if direction == "SHORT" else (q_now <= sl_q)
                if hit_tp:
                    trade.exit_time = ts
                    trade.exit_px = float(ex.close)
                    trade.exit_reason = "mr_tp"
                    trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                    trade.minutes_held = held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts={})
                if hit_sl:
                    trade.exit_time = ts
                    trade.exit_px = float(ex.close)
                    trade.exit_reason = "mr_sl"
                    trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                    trade.minutes_held = held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts={})
                if held >= int(mr_max_hold_min):
                    trade.exit_time = ts
                    trade.exit_px = float(ex.close)
                    trade.exit_reason = "mr_time_exit"
                    trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                    trade.minutes_held = held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts={})

                if ts.time() >= time(15, 59):
                    trade.exit_time = ts
                    trade.exit_px = float(ex.close)
                    trade.exit_reason = "mr_eod"
                    trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                    trade.minutes_held = held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts={})
        return DayOutcome(day=day, trade=None, no_trade_reason="mr_no_signal", reject_counts={})

    for i in range(market_open_idx, len(qqq)):
        now_tick = qqq[i]
        now = now_tick.ts.replace(second=0, microsecond=0)
        now_t = now.time()
        if min_entry_time is not None and now < min_entry_time:
            continue

        vwap_now = calculate_vwap(qqq, i)
        if vwap_now <= 0:
            continue

        new_state = state_from_vwap(now_tick.close, vwap_now, band_bps=8.0)
        if (cross_state == "ABOVE" and new_state == "BELOW") or (cross_state == "BELOW" and new_state == "ABOVE"):
            cross_count += 1
        if new_state != "MIDDLE":
            cross_state = new_state

        if not (entry_window_start <= now_t <= entry_window_end):
            continue
        if i < 14:
            reject_counts["warmup_not_ready"] += 1
            continue

        # Early-session structure gate:
        # A) trend-ready => allow normal DRIFT trigger from 09:45.
        # B) no structure => require one delayed confirmation in 09:45-10:00 based on 5m move.
        if not trend_ready:
            if (not delayed_confirmed) and time(9, 45) <= now_t <= delayed_confirm_end:
                close_5m_ago = minute_close_at_or_before(q_by_ts, now - timedelta(minutes=5))
                if day_open_px is not None and close_5m_ago is not None and atr_d > 0:
                    move_ratio_5m = abs(float(now_tick.close) - float(close_5m_ago)) / float(atr_d)
                    open_to_now_dir: Optional[str] = None
                    if float(now_tick.close) > float(day_open_px):
                        open_to_now_dir = "LONG"
                    elif float(now_tick.close) < float(day_open_px):
                        open_to_now_dir = "SHORT"
                    confirm_ok = (move_ratio_5m >= float(delayed_confirm_move5m_min))
                    # Adaptive relaxation for delayed-confirm:
                    # only relax threshold on structured (non-chop) tape.
                    if (
                        (not confirm_ok)
                        and bool(delayed_confirm_adaptive_enable)
                        and open_to_now_dir is not None
                    ):
                        vwap_dev_atr_d = abs(float(now_tick.close) - float(vwap_now)) / float(atr_d)
                        if (
                            move_ratio_5m >= float(delayed_confirm_adaptive_move5m_min)
                            and cross_count <= int(delayed_confirm_adaptive_cross_count_max)
                            and vwap_dev_atr_d >= float(delayed_confirm_adaptive_vwap_dev_atr_d_min)
                        ):
                            confirm_ok = True
                    if confirm_ok and open_to_now_dir is not None:
                        delayed_confirmed = True
                        delayed_dir_lock = open_to_now_dir
            if not delayed_confirmed:
                reject_counts["delayed_confirm_not_met"] += 1
                continue

        atr_live = calculate_atr(qqq, i, period=14)
        if atr_live is None or atr_live <= 0:
            reject_counts["atr_live_not_ready"] += 1
            continue

        # Expansion confirmation before entry:
        # abs(price - open) must be at least 0.35 * ATR.
        if day_open_px is None or abs(float(now_tick.close) - day_open_px) < (0.35 * float(atr_live)):
            reject_counts["expansion_not_enough"] += 1
            continue

        qqq_3m_ago = minute_close_at_or_before(q_by_ts, now - timedelta(minutes=3))
        if qqq_3m_ago is None:
            reject_counts["qqq_3m_missing"] += 1
            continue
        v3m_signed = float(now_tick.close - qqq_3m_ago)
        v3m_abs = abs(v3m_signed)

        direction: Optional[str] = None
        exec_symbol: Optional[str] = None
        exec_ticks: Optional[list[Tick]] = None
        exec_map: Optional[dict[datetime, Tick]] = None

        if now_tick.close > vwap_now:
            direction = "LONG"
            exec_symbol = "TQQQ"
            exec_ticks = tqqq
            exec_map = t_by_ts
        elif now_tick.close < vwap_now:
            direction = "SHORT"
            exec_symbol = "SQQQ"
            exec_ticks = sqqq
            exec_map = s_by_ts
        else:
            continue

        if narrow_day_flag and direction == "SHORT":
            reject_counts["narrow_day_skip_short"] += 1
            continue

        if (
            float(stop_reentry_cooldown_min) > 0.0
            and last_stop_exit_at is not None
            and last_stop_exit_side == direction
            and now < (last_stop_exit_at + timedelta(minutes=max(0.0, float(stop_reentry_cooldown_min))))
        ):
            reject_counts["stop_reentry_cooldown_active"] += 1
            continue

        # Conditional day circuit-breaker:
        # - fixed mode: block all entries after N hard stops
        # - chop-only mode: block only when current tape is chop-like
        if day_stop_after_hard_stops is not None and int(day_stop_after_hard_stops) > 0:
            if int(day_hard_stop_count_before) >= int(day_stop_after_hard_stops):
                if bool(day_stop_after_hard_stops_chop_only):
                    if _is_chop_realtime(i, float(vwap_now)):
                        reject_counts["day_stop_hard_stops_chop_only"] += 1
                        continue
                else:
                    reject_counts["day_stop_hard_stops"] += 1
                    continue

        force_early_entry = bool(
            early_override_direct_entry
            and
            early_override
            and early_override_direction is not None
            and direction == early_override_direction
            and (early_override_window_start <= now_t <= early_override_window_end)
        )

        cross_count_15m = _cross_count_15m(i)
        pre_second_chop_on = bool(
            pre_second_chop_guard_enable
            and trade_seq >= 2
            and pre_second_chop_window_start <= now_t <= pre_second_chop_window_end
            and cross_count_15m >= int(pre_second_chop_cross_count_15m_min)
            and ((not bool(pre_second_chop_require_first_stop)) or bool(first_trade_was_hard_stop))
        )
        if pre_second_chop_on and str(pre_second_chop_action).lower() == "skip":
            reject_counts["pre_second_chop_skip"] += 1
            continue
        day_pnl_ok_for_lock = (float(day_realized_pnl_before) <= 0.0) if bool(chop_lock_require_day_pnl_nonpositive) else True
        chop_lock_on = bool(
            chop_lock_enable
            and (chop_lock_window_start <= now_t <= chop_lock_window_end)
            and (cross_count_15m >= int(chop_lock_cross_count_15m_min))
            and day_pnl_ok_for_lock
        )

        if chop_lock_on and float(chop_lock_rearm_vwap_band_atr_d) > 0 and (not force_early_entry):
            dist_vwap_atr_d = abs(float(now_tick.close) - float(vwap_now)) / float(atr_d) if atr_d > 0 else 0.0
            if dist_vwap_atr_d > float(chop_lock_rearm_vwap_band_atr_d):
                reject_counts["chop_lock_rearm_vwap_not_met"] += 1
                continue

        if low_atr_release_active and (not force_early_entry):
            if not (low_atr_release_window_start <= now_t <= low_atr_release_window_end):
                reject_counts["low_atr_release_window"] += 1
                continue
            if cross_count > int(low_atr_release_max_cross_count):
                reject_counts["low_atr_release_cross_count"] += 1
                continue
            dist_vwap_atr_d = abs(float(now_tick.close) - float(vwap_now)) / float(atr_d) if atr_d > 0 else 0.0
            if dist_vwap_atr_d < float(low_atr_release_min_vwap_dist_atr_d):
                reject_counts["low_atr_release_vwap_dist"] += 1
                continue

        if trend_ready and not force_early_entry:
            if early_lock_mode == "breakout":
                # Trend-ready days use breakout lock:
                # after 09:45, only allow LONG when price breaks 09:40-window high,
                # only allow SHORT when price breaks 09:40-window low.
                if now_t < time(9, 45):
                    reject_counts["trend_breakout_not_ready_time"] += 1
                    continue
                if breakout_hi_0940 is None or breakout_lo_0940 is None:
                    reject_counts["trend_breakout_ref_missing"] += 1
                    continue
                if direction == "LONG" and not (float(now_tick.close) > float(breakout_hi_0940)):
                    reject_counts["trend_breakout_not_confirmed"] += 1
                    continue
                if direction == "SHORT" and not (float(now_tick.close) < float(breakout_lo_0940)):
                    reject_counts["trend_breakout_not_confirmed"] += 1
                    continue
            else:
                if early_dir_lock is not None and direction != early_dir_lock:
                    reject_counts["early_direction_lock_mismatch"] += 1
                    continue
        else:
            if delayed_dir_lock is not None and direction != delayed_dir_lock:
                reject_counts["delayed_direction_lock_mismatch"] += 1
                continue

        exec_now = now
        q_entry_ref = now_tick
        if (not force_early_entry) and bool(breakout_followthrough_confirm):
            confirm_ts = now + timedelta(minutes=1)
            confirm_tick = q_by_ts.get(confirm_ts)
            if confirm_tick is None:
                reject_counts["breakout_followthrough_missing"] += 1
                continue
            # Breakout follow-through confirmation:
            # LONG: next close >= signal bar high
            # SHORT: next close <= signal bar low
            if direction == "LONG":
                if float(confirm_tick.close) < float(now_tick.high):
                    reject_counts["breakout_followthrough_failed"] += 1
                    continue
            else:
                if float(confirm_tick.close) > float(now_tick.low):
                    reject_counts["breakout_followthrough_failed"] += 1
                    continue
            exec_now = confirm_ts
            q_entry_ref = confirm_tick

        if (not force_early_entry) and int(entry_confirm_delay_min) > 0:
            confirm_ts = now + timedelta(minutes=int(entry_confirm_delay_min))
            q_confirm = q_by_ts.get(confirm_ts)
            if q_confirm is None:
                reject_counts["entry_confirm_missing"] += 1
                continue
            i_confirm = idx_by_ts.get(confirm_ts)
            if i_confirm is None:
                reject_counts["entry_confirm_missing"] += 1
                continue
            vwap_confirm = calculate_vwap(qqq, i_confirm)
            if vwap_confirm <= 0:
                reject_counts["entry_confirm_missing"] += 1
                continue
            # Minimal anti-fast-reversal guard: wait N minutes and require trend side still intact.
            if direction == "LONG":
                if not (float(q_confirm.close) >= float(now_tick.close) and float(q_confirm.close) > float(vwap_confirm)):
                    reject_counts["entry_confirm_failed"] += 1
                    continue
            else:
                if not (float(q_confirm.close) <= float(now_tick.close) and float(q_confirm.close) < float(vwap_confirm)):
                    reject_counts["entry_confirm_failed"] += 1
                    continue
            exec_now = confirm_ts
            q_entry_ref = q_confirm

        if anti_chase_chop_enable and (not force_early_entry):
            ttl_sec = max(0, int(anti_chase_signal_ttl_sec))
            if (exec_now - now).total_seconds() > float(ttl_sec):
                reject_counts["signal_ttl_expired"] += 1
                continue

        exec_tick_now = exec_map.get(exec_now)
        if exec_tick_now is None:
            reject_counts["exec_price_missing"] += 1
            continue

        # Optional first-trade filters for entry quality diagnostics.
        is_first_trade_of_day = (min_entry_time is None)
        if is_first_trade_of_day and bool(first_trade_block_0945_0950):
            if time(9, 45) <= exec_now.time() <= time(9, 50):
                reject_counts["first_trade_blocked_0945_0950"] += 1
                continue

        entry_breaks_0940_extreme: Optional[bool] = None
        entry_dist_0940: Optional[float] = None
        entry_dist_0940_atr_d: Optional[float] = None
        second_test_before_entry = False
        failed_breakout_pullback_before_entry = False
        if breakout_hi_0940 is not None and breakout_lo_0940 is not None:
            if direction == "LONG":
                entry_breaks_0940_extreme = bool(float(q_entry_ref.close) > float(breakout_hi_0940))
                entry_dist_0940 = float(q_entry_ref.close) - float(breakout_hi_0940)
            else:
                entry_breaks_0940_extreme = bool(float(q_entry_ref.close) < float(breakout_lo_0940))
                entry_dist_0940 = float(breakout_lo_0940) - float(q_entry_ref.close)
            if atr_d > 0:
                entry_dist_0940_atr_d = float(entry_dist_0940 / float(atr_d))

            qpre = [t for t in qqq if time(9, 45) <= t.ts.time() <= exec_now.time()]
            if direction == "LONG":
                touches = sum(1 for t in qpre if float(t.high) >= float(breakout_hi_0940))
            else:
                touches = sum(1 for t in qpre if float(t.low) <= float(breakout_lo_0940))
            second_test_before_entry = touches >= 2

            qpre2 = [t for t in qqq if time(9, 45) <= t.ts.time() < exec_now.time()]
            if qpre2:
                if direction == "LONG":
                    first_break = next((k for k, t in enumerate(qpre2) if float(t.high) > float(breakout_hi_0940)), None)
                    if first_break is not None:
                        failed_breakout_pullback_before_entry = any(
                            float(t.close) < float(breakout_hi_0940) for t in qpre2[first_break + 1:]
                        )
                else:
                    first_break = next((k for k, t in enumerate(qpre2) if float(t.low) < float(breakout_lo_0940)), None)
                    if first_break is not None:
                        failed_breakout_pullback_before_entry = any(
                            float(t.close) > float(breakout_lo_0940) for t in qpre2[first_break + 1:]
                        )

        if is_first_trade_of_day and bool(first_trade_second_test_require_breakout):
            if second_test_before_entry and (entry_breaks_0940_extreme is False):
                reject_counts["second_test_without_breakout"] += 1
                continue

        if is_first_trade_of_day and bool(first_trade_skip_failed_breakout_pullback):
            if failed_breakout_pullback_before_entry:
                reject_counts["failed_breakout_pullback"] += 1
                continue

        if (
            is_first_trade_of_day
            and entry_dist_0940_atr_d is not None
            and entry_dist_0940_atr_d > 0
            and entry_dist_0940_max_atr_d is not None
            and float(entry_dist_0940_atr_d) > float(entry_dist_0940_max_atr_d)
        ):
            reject_counts["entry_overextended_0940"] += 1
            continue

        # Use the same persisted pre-seed source as live strategy.
        exec_idx = next((k for k, t in enumerate(exec_ticks) if t.ts.replace(second=0, microsecond=0) == exec_now), None)
        if exec_idx is None or exec_idx < 14:
            reject_counts["exec_idx_not_ready"] += 1
            continue
        # Live entry gate uses TQQQ atr_seed for both LONG/SHORT paths.
        atr_seed = atr_seed_for_day(atr_seed_map, day, "TQQQ")
        if atr_seed is None or atr_seed <= 0:
            reject_counts["atr_seed_missing"] += 1
            continue

        atr_eff = min(float(atr_live), float(atr_cap_mult * atr_seed))
        if atr_eff <= 0:
            reject_counts["atr_eff_invalid"] += 1
            continue

        sign_ok = force_early_entry or (
            (direction == "LONG" and v3m_signed > momentum_deadzone)
            or (direction == "SHORT" and v3m_signed < -momentum_deadzone)
        )
        if not sign_ok:
            reject_counts["sign_mismatch"] += 1
            continue

        vol_sum = sum(t.volume for t in qqq[max(0, i - 10):i])
        vol_ma10 = vol_sum / max(1, min(10, i))
        vol_ratio = (now_tick.volume / vol_ma10) if vol_ma10 > 0 else 0.0

        v3m_atr = v3m_abs / max(atr_eff, 1e-9)

        if time(9, 40) <= exec_now.time() <= time(11, 50):
            ref_30s = minute_close_at_or_before(q_by_ts, exec_now - timedelta(seconds=30))
            ref_120s = minute_close_at_or_before(q_by_ts, exec_now - timedelta(seconds=120))
            if ref_30s is not None:
                if direction == "LONG" and q_entry_ref.close < ref_30s:
                    reject_counts["veto_30s"] += 1
                    continue
                if direction == "SHORT" and q_entry_ref.close > ref_30s:
                    reject_counts["veto_30s"] += 1
                    continue
            if ref_120s is not None:
                move_120s_live_atr = (q_entry_ref.close - ref_120s) / max(float(atr_live), 1e-9)
                if direction == "LONG" and move_120s_live_atr < -veto_rebound_live_atr:
                    reject_counts["veto_120s"] += 1
                    continue
                if direction == "SHORT" and move_120s_live_atr > veto_rebound_live_atr:
                    reject_counts["veto_120s"] += 1
                    continue

        if cross_count > fast_crosses_threshold and cross_count > fast_cooldown_trigger_cross:
            fast_cooldown_trigger_cross = int(cross_count)
            fast_cooldown_until = now + timedelta(minutes=max(1.0, fast_cooldown_min))

        fast_in_cooldown = fast_cooldown_until is not None and now < fast_cooldown_until
        fast_ok = sign_ok and (v3m_atr >= fast_v3m_atr_min) and (vol_ratio >= fast_vol_min) and (not fast_in_cooldown)

        c0 = minute_close_at_or_before(q_by_ts, now)
        c1 = minute_close_at_or_before(q_by_ts, now - timedelta(minutes=1))
        c2 = minute_close_at_or_before(q_by_ts, now - timedelta(minutes=2))
        c3 = minute_close_at_or_before(q_by_ts, now - timedelta(minutes=3))
        drift_ok = False
        if c0 is not None and c1 is not None and c2 is not None and c3 is not None:
            bar_th = drift_bar_min_atr * atr_eff
            drift_cum_req = float(drift_cum_atr_min)
            if low_atr_release_active and (not force_early_entry):
                bar_th = float(low_atr_release_drift_bar_min_atr) * float(atr_eff)
                drift_cum_req = float(low_atr_release_drift_cum_atr_min)
            d1 = c0 - c1
            d2 = c1 - c2
            d3 = c2 - c3
            if direction == "LONG":
                drift_ok = d1 >= bar_th and d2 >= bar_th and d3 >= bar_th and (c0 - c3) >= (drift_cum_req * atr_eff)
            else:
                drift_ok = (-d1) >= bar_th and (-d2) >= bar_th and (-d3) >= bar_th and (-(c0 - c3)) >= (drift_cum_req * atr_eff)

        if low_atr_release_active and (not force_early_entry):
            fast_ok = False
            if not drift_ok:
                reject_counts["low_atr_release_drift_only"] += 1
                continue

        if (not force_early_entry) and (not fast_ok and not drift_ok):
            reject_counts["fast_drift_not_ready"] += 1
            continue

        # Focused anti-fake-break guard for the opening push:
        # For LONG+DRIFT signals in 09:45-10:29, require one extra minute of
        # continuation and trend-side integrity before entering.
        confirm_side = str(long_drift_second_confirm_side).lower()
        confirm_side_ok = (direction == "LONG") if confirm_side == "long" else (direction in {"LONG", "SHORT"})
        if (
            bool(long_drift_second_confirm)
            and (not force_early_entry)
            and confirm_side_ok
            and drift_ok
            and (long_drift_second_confirm_window_start <= exec_now.time() <= long_drift_second_confirm_window_end)
        ):
            c_delay = max(1, int(long_drift_second_confirm_delay_min))
            c_ts = exec_now + timedelta(minutes=c_delay)
            c_tick = q_by_ts.get(c_ts)
            c_idx = idx_by_ts.get(c_ts)
            if c_tick is None or c_idx is None:
                reject_counts["long_drift_second_confirm_missing"] += 1
                continue
            c_vwap = calculate_vwap(qqq, c_idx)
            if c_vwap <= 0:
                reject_counts["long_drift_second_confirm_missing"] += 1
                continue
            if direction == "LONG":
                if not (float(c_tick.close) >= float(q_entry_ref.close) and float(c_tick.close) > float(c_vwap)):
                    reject_counts["long_drift_second_confirm_failed"] += 1
                    continue
            else:
                if not (float(c_tick.close) <= float(q_entry_ref.close) and float(c_tick.close) < float(c_vwap)):
                    reject_counts["long_drift_second_confirm_failed"] += 1
                    continue
            exec_now = c_ts
            q_entry_ref = c_tick

        # DRIFT is primary; FAST is only used as a fallback.
        if force_early_entry:
            entry_mode = "EARLY_OVERRIDE"
        elif low_atr_release_active:
            entry_mode = "LOW_ATR_RELEASE"
        else:
            entry_mode = "DRIFT" if drift_ok else "FAST"

        if anti_choptop_enable and (not force_early_entry):
            if anti_choptop_window_start <= now_t <= anti_choptop_window_end:
                apply_to = str(anti_choptop_apply_to).lower()
                mode_ok = (apply_to == "all") or (apply_to == "drift" and entry_mode in {"DRIFT", "LOW_ATR_RELEASE"})
                if mode_ok:
                    ttl_until = anti_choptop_block_until.get(direction)
                    if ttl_until is not None and now < ttl_until:
                        reject_counts["anti_choptop_ttl_active"] += 1
                        continue
                    rank_15m = _rank_15m(i)
                    if rank_15m is not None and atr_d > 0:
                        sign_flip_5m = _sign_flip_5m(i)
                        dev_atr_d = abs(float(now_tick.close) - float(vwap_now)) / float(atr_d)
                        is_top = (
                            (direction == "LONG" and rank_15m >= float(anti_choptop_rank_high))
                            or (direction == "SHORT" and rank_15m <= float(anti_choptop_rank_low))
                        )
                        if (
                            is_top
                            and sign_flip_5m >= int(anti_choptop_sign_flip_5m_min)
                            and dev_atr_d >= float(anti_choptop_dev_atr_d_min)
                        ):
                            anti_choptop_block_until[direction] = now + timedelta(minutes=max(1, int(anti_choptop_ttl_min)))
                            reject_counts["anti_choptop_block"] += 1
                            continue

        if anti_chase_chop_enable and (not force_early_entry):
            if chop_lock_enable and (not chop_lock_on):
                pass
            else:
                apply_to = str(anti_chase_apply_to).lower()
                mode_match = (
                    apply_to == "all"
                    or (apply_to == "fast" and entry_mode == "FAST")
                    or (apply_to == "drift" and entry_mode in {"DRIFT", "LOW_ATR_RELEASE"})
                )
                if mode_match and (anti_chase_window_start <= now_t <= anti_chase_window_end):
                    if _is_chop_realtime(i, float(vwap_now)):
                        rank_15m = _rank_15m(i)
                        dist_vwap_atr_d = abs(float(now_tick.close) - float(vwap_now)) / float(atr_d) if atr_d > 0 else 0.0
                        rank_th = float(anti_chase_rank15_min)
                        dist_th = float(anti_chase_dist_vwap_atr_d_min)
                        if entry_mode == "FAST":
                            if anti_chase_fast_rank15_min is not None:
                                rank_th = float(anti_chase_fast_rank15_min)
                            if anti_chase_fast_dist_vwap_atr_d_min is not None:
                                dist_th = float(anti_chase_fast_dist_vwap_atr_d_min)
                        elif entry_mode in {"DRIFT", "LOW_ATR_RELEASE"}:
                            if anti_chase_drift_rank15_min is not None:
                                rank_th = float(anti_chase_drift_rank15_min)
                            if anti_chase_drift_dist_vwap_atr_d_min is not None:
                                dist_th = float(anti_chase_drift_dist_vwap_atr_d_min)
                        if (
                            rank_15m is not None
                            and rank_15m >= rank_th
                            and dist_vwap_atr_d >= dist_th
                        ):
                            # Anti-Chase hit: cancel this signal immediately.
                            # No auto-delayed fill on the same signal; wait for re-armed future signal.
                            reject_counts["anti_chase_cancel"] += 1
                            continue

        # exec_now may be moved by confirmation guards; refresh execution tick.
        exec_tick_now = exec_map.get(exec_now)
        if exec_tick_now is None:
            reject_counts["exec_price_missing"] += 1
            continue
        effective_notional = float(notional_usd) * float(trend_size_mult)
        if pre_second_chop_on and str(pre_second_chop_action).lower() == "degrade":
            effective_notional *= max(0.05, min(1.0, float(pre_second_chop_probe_ratio)))
        if chop_lock_on and bool(chop_lock_disable_add):
            probe_ratio = max(0.05, min(1.0, float(chop_lock_probe_ratio)))
            effective_notional *= probe_ratio
        qty = int(effective_notional / exec_tick_now.close)
        if qty <= 0:
            reject_counts["qty_zero"] += 1
            continue

        trade = Trade(
            entry_time=exec_now,
            entry_px=float(exec_tick_now.close),
            entry_mode=entry_mode,
            entry_direction=direction,
            exec_symbol=exec_symbol,
            qty=qty,
            notional=qty * float(exec_tick_now.close),
            atr_eff_at_entry=float(atr_eff),
            atr_entry_used=0.0,
            vwap_crosses=int(cross_count),
            is_first_trade=is_first_trade_of_day,
            day_realized_pnl_before=float(day_realized_pnl_before),
            chop_lock_on_entry=bool(chop_lock_on),
        )

        atr_entry_used = effective_atr_backtest(
            symbol=exec_symbol,
            symbol_ticks=exec_ticks,
            idx=exec_idx,
            atr_seed=atr_seed,
            atr_cap_mult=atr_cap_mult,
            now=exec_now,
            period=14,
        )
        if atr_entry_used is None or atr_entry_used <= 0:
            reject_counts["atr_entry_missing"] += 1
            continue
        trade.atr_entry_used = float(atr_entry_used)

        # Execution is always long inventory on leveraged ETFs:
        # LONG -> buy TQQQ, SHORT -> buy SQQQ.
        # So target/PnL are always evaluated on rising execution price.
        target_px = round(trade.entry_px + (target_atr_mult * trade.atr_entry_used), 4)
        target_trend_px = (
            round(trade.entry_px + (tp2_trend_atr_mult * trade.atr_entry_used), 4)
            if tp2_trend_atr_mult > target_atr_mult
            else None
        )
        stop_dist = max(hard_stop_atr_mult * trade.atr_entry_used, stop_min_abs)
        stop_px = round(trade.entry_px - stop_dist, 4)
        trade.target_px = target_px
        entry_q_idx = idx_by_ts.get(exec_now)
        entry_q_px = float(q_entry_ref.close)
        trigger_bar_mid = 0.5 * (float(q_entry_ref.high) + float(q_entry_ref.low))
        anchor_low = float(q_entry_ref.low)
        anchor_high = float(q_entry_ref.high)
        if entry_q_idx is not None and (entry_q_idx + 1) < len(qqq):
            anchor_low = min(anchor_low, float(qqq[entry_q_idx + 1].low))
            anchor_high = max(anchor_high, float(qqq[entry_q_idx + 1].high))
        q_mfe_abs = 0.0
        q_mae_abs = 0.0
        qty_stage1 = max(1, qty // 2)
        qty_stage2 = qty - qty_stage1
        trade.target_pnl_if_hit = round(abs(target_px - trade.entry_px) * qty_stage1, 2)
        realized_stage1 = 0.0
        stage1_hit = False
        stage1_anchor: Optional[float] = None
        stage2_floor_px = round(trade.entry_px + (0.2 * trade.atr_entry_used), 4)

        for j in range(exec_idx + 1, len(exec_ticks)):
            ex = exec_ticks[j]
            ts = ex.ts.replace(second=0, microsecond=0)
            minutes_held = int((ts - trade.entry_time).total_seconds() // 60)
            q_idx = idx_by_ts.get(ts)
            q_tick = q_by_ts.get(ts)

            # Track early path quality in first 5 minutes using QQQ/ATR_d scale.
            if q_tick is not None and atr_d > 0 and minutes_held <= 5:
                if direction == "LONG":
                    favorable = float(q_tick.close) - entry_q_px
                    adverse = entry_q_px - float(q_tick.close)
                else:
                    favorable = entry_q_px - float(q_tick.close)
                    adverse = float(q_tick.close) - entry_q_px
                q_mfe_abs = max(q_mfe_abs, max(0.0, favorable))
                q_mae_abs = max(q_mae_abs, max(0.0, adverse))
                trade.mfe_5m = round(q_mfe_abs / float(atr_d), 4)
                trade.mae_5m = round(q_mae_abs / float(atr_d), 4)

            if not stage1_hit and ex.close >= target_px:
                realized_stage1 = round((target_px - trade.entry_px) * qty_stage1, 2)
                if qty_stage2 <= 0:
                    trade.exit_time = ts
                    trade.exit_px = target_px
                    trade.exit_reason = "target_hit_full"
                    trade.exec_pnl = float(realized_stage1)
                    trade.minutes_held = minutes_held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))
                stage1_hit = True
                stage1_anchor = float(ex.close)
                continue

            if (
                str(fast_reversal_guard).lower() != "none"
                and not stage1_hit
                and q_tick is not None
                and q_idx is not None
                and atr_d > 0
                and (q_mfe_abs < (0.20 * float(atr_d)))
            ):
                guard_mode = str(fast_reversal_guard).lower()
                guard_hit = False
                if guard_mode == "v1" and 2 <= minutes_held <= 4 and q_idx >= 1:
                    q_now = qqq[q_idx]
                    q_prev = qqq[q_idx - 1]
                    body_sum = abs(float(q_now.close) - float(q_now.open)) + abs(float(q_prev.close) - float(q_prev.open))
                    if direction == "LONG":
                        b1 = (
                            float(q_now.close) < float(q_now.open)
                            and float(q_prev.close) < float(q_prev.open)
                            and body_sum >= (0.10 * float(atr_d))
                        )
                        b2 = float(q_now.close) < trigger_bar_mid and float(q_prev.close) < trigger_bar_mid
                        vwap_now = calculate_vwap(qqq, q_idx)
                        vwap_prev = calculate_vwap(qqq, q_idx - 1)
                        b3 = (
                            float(q_now.close) < float(vwap_now)
                            and float(q_prev.close) < float(vwap_prev)
                            and float(q_now.high) < float(vwap_now)
                        )
                    else:
                        b1 = (
                            float(q_now.close) > float(q_now.open)
                            and float(q_prev.close) > float(q_prev.open)
                            and body_sum >= (0.10 * float(atr_d))
                        )
                        b2 = float(q_now.close) > trigger_bar_mid and float(q_prev.close) > trigger_bar_mid
                        vwap_now = calculate_vwap(qqq, q_idx)
                        vwap_prev = calculate_vwap(qqq, q_idx - 1)
                        b3 = (
                            float(q_now.close) > float(vwap_now)
                            and float(q_prev.close) > float(vwap_prev)
                            and float(q_now.low) > float(vwap_now)
                        )
                    guard_hit = bool(b1 or b2 or b3)

                if guard_mode == "v2" and 2 <= minutes_held <= 5:
                    if direction == "LONG":
                        guard_hit = float(q_tick.close) < float(anchor_low)
                    else:
                        guard_hit = float(q_tick.close) > float(anchor_high)

                if guard_mode == "v3":
                    # Narrow trigger scope:
                    # - optional first trade only
                    # - entry time window
                    # - DRIFT only
                    # - entry close to 09:40 breakout edge
                    v3_scope_ok = True
                    if bool(fast_reversal_v3_only_first_trade) and (not bool(trade.is_first_trade)):
                        v3_scope_ok = False
                    if not (fast_reversal_v3_window_start <= trade.entry_time.time() <= fast_reversal_v3_window_end):
                        v3_scope_ok = False
                    if trade.entry_mode != "DRIFT":
                        v3_scope_ok = False
                    if (
                        entry_dist_0940_atr_d is None
                        or entry_dist_0940_atr_d < 0
                        or float(entry_dist_0940_atr_d) > float(fast_reversal_v3_max_entry_dist_atr_d)
                    ):
                        v3_scope_ok = False

                    if v3_scope_ok and 2 <= minutes_held <= 4 and q_idx >= 1:
                        q_now = qqq[q_idx]
                        q_prev = qqq[q_idx - 1]
                        body_sum = abs(float(q_now.close) - float(q_now.open)) + abs(float(q_prev.close) - float(q_prev.open))
                        if direction == "LONG":
                            b1 = (
                                float(q_now.close) < float(q_now.open)
                                and float(q_prev.close) < float(q_prev.open)
                                and body_sum >= (0.10 * float(atr_d))
                            )
                            b2 = float(q_now.close) < trigger_bar_mid and float(q_prev.close) < trigger_bar_mid
                            vwap_now = calculate_vwap(qqq, q_idx)
                            vwap_prev = calculate_vwap(qqq, q_idx - 1)
                            b3 = (
                                float(q_now.close) < float(vwap_now)
                                and float(q_prev.close) < float(vwap_prev)
                                and float(q_now.high) < float(vwap_now)
                            )
                        else:
                            b1 = (
                                float(q_now.close) > float(q_now.open)
                                and float(q_prev.close) > float(q_prev.open)
                                and body_sum >= (0.10 * float(atr_d))
                            )
                            b2 = float(q_now.close) > trigger_bar_mid and float(q_prev.close) > trigger_bar_mid
                            vwap_now = calculate_vwap(qqq, q_idx)
                            vwap_prev = calculate_vwap(qqq, q_idx - 1)
                            b3 = (
                                float(q_now.close) > float(vwap_now)
                                and float(q_prev.close) > float(vwap_prev)
                                and float(q_now.low) > float(vwap_now)
                            )
                        guard_hit = bool(b1 or b2 or b3)

                if guard_hit:
                    trade.exit_time = ts
                    trade.exit_px = float(ex.close)
                    if guard_mode == "v2":
                        trade.exit_reason = "fast_reversal_anchor_break_exit"
                    elif guard_mode == "v3":
                        trade.exit_reason = "fast_reversal_guard_v3"
                    else:
                        trade.exit_reason = "fast_reversal_guard_v1"
                    trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                    trade.minutes_held = minutes_held
                    trade.fast_reversal_guard_hit = True
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

            if (
                trade.entry_mode == "EARLY_OVERRIDE"
                and early_quick_exit_min is not None
                and int(early_quick_exit_min) > 0
                and not stage1_hit
                and minutes_held >= int(early_quick_exit_min)
                and atr_d > 0
                and q_mfe_abs < (float(early_continuation_min_atr_d) * float(atr_d))
            ):
                trade.exit_time = ts
                trade.exit_px = float(ex.close)
                trade.exit_reason = "early_no_continuation_exit"
                trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                trade.minutes_held = minutes_held
                return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

            if (
                trade.entry_mode == "LOW_ATR_RELEASE"
                and int(low_atr_release_quick_exit_min) > 0
                and not stage1_hit
                and minutes_held >= int(low_atr_release_quick_exit_min)
                and atr_d > 0
                and q_mfe_abs < (float(low_atr_release_continuation_min_atr_d) * float(atr_d))
            ):
                trade.exit_time = ts
                trade.exit_px = float(ex.close)
                trade.exit_reason = "low_atr_no_continuation_exit"
                trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                trade.minutes_held = minutes_held
                return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

            # Hard stop delay: disabled for the first 3 minutes after entry.
            if not stage1_hit and hard_stop_atr_mult > 0 and minutes_held >= 3 and ex.close <= stop_px:
                trade.exit_time = ts
                trade.exit_px = stop_px
                trade.exit_reason = "hard_stop_hit"
                trade.exec_pnl = round((stop_px - trade.entry_px) * qty, 2)
                trade.minutes_held = minutes_held
                return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

            if stage1_hit:
                if target_trend_px is not None and ex.close >= target_trend_px:
                    trade.exit_time = ts
                    trade.exit_px = target_trend_px
                    trade.exit_reason = "tp1_then_tp2_trend_hit"
                    tail_pnl = round((target_trend_px - trade.entry_px) * qty_stage2, 2)
                    trade.exec_pnl = round(realized_stage1 + tail_pnl, 2)
                    trade.minutes_held = minutes_held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))
                stage1_anchor = max(float(stage1_anchor or ex.close), float(ex.close))
                trail_px = max(stage2_floor_px, round(float(stage1_anchor) - (0.5 * trade.atr_entry_used), 4))
                if ex.close <= trail_px:
                    trade.exit_time = ts
                    trade.exit_px = trail_px
                    trade.exit_reason = "tp1_then_trailing_stop"
                    tail_pnl = round((trail_px - trade.entry_px) * qty_stage2, 2)
                    trade.exec_pnl = round(realized_stage1 + tail_pnl, 2)
                    trade.minutes_held = minutes_held
                    return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))
            elif minutes_held >= logic_retreat_minutes:
                q_tick = q_by_ts.get(ts)
                if q_tick is not None:
                    q_idx = next((k for k, t in enumerate(qqq) if t.ts.replace(second=0, microsecond=0) == ts), None)
                    if q_idx is not None:
                        vwap_rt = calculate_vwap(qqq, q_idx)
                        qqq_now = q_tick.close
                        atr_q_now = calculate_atr(qqq, q_idx, period=14)
                        atr_buffer = max(0.0, float(logic_retreat_buffer_atr)) * float(atr_q_now or 0.0)
                        long_retreat_line = vwap_rt - atr_buffer
                        short_retreat_line = vwap_rt + atr_buffer
                        wrong_side = (direction == "LONG" and qqq_now < long_retreat_line) or (direction == "SHORT" and qqq_now > short_retreat_line)
                        if wrong_side:
                            trade.exit_time = ts
                            trade.exit_px = float(ex.close)
                            trade.exit_reason = "logic_retreat"
                            trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                            trade.minutes_held = minutes_held
                            return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

            if ts.time() >= time(15, 59):
                trade.exit_time = ts
                trade.exit_px = float(ex.close)
                if stage1_hit:
                    trade.exit_reason = "tp1_then_eod"
                    tail_pnl = round((trade.exit_px - trade.entry_px) * qty_stage2, 2)
                    trade.exec_pnl = round(realized_stage1 + tail_pnl, 2)
                else:
                    trade.exit_reason = "force_flatten_1559_EOD"
                    trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
                trade.minutes_held = minutes_held
                return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

        last = exec_ticks[-1]
        trade.exit_time = last.ts.replace(second=0, microsecond=0)
        trade.exit_px = float(last.close)
        if stage1_hit:
            trade.exit_reason = "tp1_then_eod_no_exit"
            tail_pnl = round((trade.exit_px - trade.entry_px) * qty_stage2, 2)
            trade.exec_pnl = round(realized_stage1 + tail_pnl, 2)
        else:
            trade.exit_reason = "eod_no_exit"
            trade.exec_pnl = round((trade.exit_px - trade.entry_px) * qty, 2)
        trade.minutes_held = int((trade.exit_time - trade.entry_time).total_seconds() // 60)
        return DayOutcome(day=day, trade=trade, no_trade_reason="", reject_counts=dict(reject_counts))

    if reject_counts:
        no_trade_reason = max(reject_counts.items(), key=lambda x: x[1])[0]
    else:
        no_trade_reason = "no_signal"
    return DayOutcome(day=day, trade=None, no_trade_reason=no_trade_reason, reject_counts=dict(reject_counts))


def iter_calendar_days(start: date, end: date) -> list[str]:
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="v47_fast_drift aligned backtest")
    parser.add_argument("--start-date", default="2025-10-01")
    parser.add_argument("--end-date", default="2026-02-28")
    parser.add_argument("--notional-usd", type=float, default=10000.0)
    parser.add_argument("--target-atr", type=float, default=1.0)
    parser.add_argument("--tp2-trend-atr", type=float, default=0.0)
    parser.add_argument("--hard-stop-atr", type=float, default=0.9)
    parser.add_argument("--stop-min-abs", type=float, default=0.50)
    parser.add_argument("--logic-retreat-min", type=int, default=7)
    parser.add_argument("--logic-retreat-buffer-atr", type=float, default=0.0)
    parser.add_argument("--atr-cap-mult", type=float, default=1.8)
    parser.add_argument("--atr-seed-path", default=str(REPO_ROOT / "runtime/state/qqq_intraday_atr_seed.json"))
    parser.add_argument("--momentum-deadzone", type=float, default=0.05)
    parser.add_argument("--fast-v3m-atr-min", type=float, default=0.60)
    parser.add_argument("--fast-vol-min", type=float, default=1.50)
    parser.add_argument("--drift-bar-min-atr", type=float, default=0.10)
    parser.add_argument("--drift-cum-atr-min", type=float, default=0.45)
    parser.add_argument("--fast-crosses-threshold", type=int, default=3)
    parser.add_argument("--fast-cooldown-min", type=float, default=15.0)
    parser.add_argument("--veto-rebound-live-atr", type=float, default=0.25)
    parser.add_argument("--atr-rank-chop-max", type=float, default=0.30)
    parser.add_argument("--early-breakout-move-atr", type=float, default=0.70)
    parser.add_argument("--early-breakout-vol-min", type=float, default=1.50)
    parser.add_argument("--early-breakout-vol-min-mode", choices=["fixed", "dynamic"], default="fixed")
    parser.add_argument("--early-breakout-vol-lookback", type=int, default=60)
    parser.add_argument("--early-breakout-vol-quantile", type=float, default=0.50)
    parser.add_argument("--early-breakout-vol-min-clamp", type=float, default=0.55)
    parser.add_argument("--early-breakout-vol-max-clamp", type=float, default=0.85)
    parser.add_argument("--chop-mode", choices=["mr", "skip"], default="mr")
    parser.add_argument("--mr-tp-atr", type=float, default=0.50)
    parser.add_argument("--mr-sl-atr", type=float, default=0.80)
    parser.add_argument("--mr-max-hold-min", type=int, default=15)
    parser.add_argument("--a-day-size-mult", type=float, default=1.50)
    parser.add_argument("--a-day-atr-rank-min", type=float, default=0.70)
    parser.add_argument("--drift-cum-atrd-min", type=float, default=0.70)
    parser.add_argument("--early-lock-mode", choices=["openclose", "breakout"], default="openclose")
    parser.add_argument("--max-trades-per-day", type=int, default=3)
    parser.add_argument("--second-trade-require-first-win", action="store_true")
    parser.add_argument("--second-trade-first-min-r", type=float, default=None)
    parser.add_argument("--third-trade-require-first2-positive", action="store_true")
    parser.add_argument("--first-loss-allow-only-one-extra", action="store_true")
    parser.add_argument("--day-stop-after-hard-stops", type=int, default=None)
    parser.add_argument("--day-stop-after-hard-stops-chop-only", action="store_true")
    parser.add_argument("--stop-reentry-cooldown-min", type=float, default=0.0)
    parser.add_argument("--pre-second-chop-guard-enable", action="store_true")
    parser.add_argument("--pre-second-chop-window-start", default="09:45")
    parser.add_argument("--pre-second-chop-window-end", default="14:00")
    parser.add_argument("--pre-second-chop-cross-count-15m-min", type=int, default=3)
    parser.add_argument("--pre-second-chop-require-first-stop", action="store_true")
    parser.add_argument("--pre-second-chop-action", choices=["degrade", "skip"], default="degrade")
    parser.add_argument("--pre-second-chop-probe-ratio", type=float, default=0.50)
    parser.add_argument("--day-stop-after-consecutive-losses", type=int, default=None)
    parser.add_argument("--delayed-confirm-move5m-min", type=float, default=0.155)
    parser.add_argument("--delayed-confirm-end", default="10:00")
    parser.add_argument("--delayed-confirm-adaptive-enable", action="store_true")
    parser.add_argument("--delayed-confirm-adaptive-move5m-min", type=float, default=0.08)
    parser.add_argument("--delayed-confirm-adaptive-cross-count-max", type=int, default=6)
    parser.add_argument("--delayed-confirm-adaptive-vwap-dev-atr-d-min", type=float, default=0.12)
    parser.add_argument("--entry-confirm-delay-min", type=int, default=0)
    parser.add_argument("--long-drift-second-confirm", action="store_true")
    parser.add_argument("--long-drift-second-confirm-side", choices=["long", "both"], default="long")
    parser.add_argument("--long-drift-second-confirm-window-start", default="09:45")
    parser.add_argument("--long-drift-second-confirm-window-end", default="10:29")
    parser.add_argument("--long-drift-second-confirm-delay-min", type=int, default=1)
    parser.add_argument("--breakout-followthrough-confirm", action="store_true")
    parser.add_argument("--skip-short-on-narrow-day", action="store_true")
    parser.add_argument("--narrow-open-range-atr-max", type=float, default=0.55)
    parser.add_argument("--first-trade-block-0945-0950", action="store_true")
    parser.add_argument("--first-trade-second-test-require-breakout", action="store_true")
    parser.add_argument("--first-trade-skip-failed-breakout-pullback", action="store_true")
    parser.add_argument("--entry-dist-0940-max-atr-d", type=float, default=None)
    parser.add_argument("--fast-reversal-guard", choices=["none", "v1", "v2", "v3"], default="none")
    parser.add_argument("--fast-reversal-v3-only-first-trade", action="store_true")
    parser.add_argument("--fast-reversal-v3-window-start", default="09:45")
    parser.add_argument("--fast-reversal-v3-window-end", default="10:05")
    parser.add_argument("--fast-reversal-v3-max-entry-dist-atr-d", type=float, default=0.05)
    parser.add_argument("--early-override-direct-entry", action="store_true")
    parser.add_argument("--early-override-window-start", default="09:39")
    parser.add_argument("--early-override-window-end", default="09:45")
    parser.add_argument("--early-override-max-counter-bars", type=int, default=0)
    parser.add_argument("--early-override-last3-confirm", action="store_true")
    parser.add_argument("--early-override-min-vwap-dist-atr-d", type=float, default=0.0)
    parser.add_argument("--early-override-max-cross-count", type=int, default=999)
    parser.add_argument("--early-override-disable-add", action="store_true")
    parser.add_argument("--early-quick-exit-min", type=int, default=None)
    parser.add_argument("--early-continuation-min-atr-d", type=float, default=0.0)
    parser.add_argument("--low-atr-release-enable", action="store_true")
    parser.add_argument("--low-atr-release-window-start", default="10:00")
    parser.add_argument("--low-atr-release-window-end", default="11:30")
    parser.add_argument("--low-atr-release-max-cross-count", type=int, default=1)
    parser.add_argument("--low-atr-release-min-vwap-dist-atr-d", type=float, default=0.18)
    parser.add_argument("--low-atr-release-drift-bar-min-atr", type=float, default=0.10)
    parser.add_argument("--low-atr-release-drift-cum-atr-min", type=float, default=0.55)
    parser.add_argument("--low-atr-release-quick-exit-min", type=int, default=2)
    parser.add_argument("--low-atr-release-continuation-min-atr-d", type=float, default=0.15)
    parser.add_argument("--chop-lock-enable", action="store_true")
    parser.add_argument("--chop-lock-window-start", default="09:45")
    parser.add_argument("--chop-lock-window-end", default="14:00")
    parser.add_argument("--chop-lock-cross-count-15m-min", type=int, default=3)
    parser.add_argument("--chop-lock-require-day-pnl-nonpositive", action="store_true")
    parser.add_argument("--chop-lock-disable-add", action="store_true")
    parser.add_argument("--chop-lock-probe-ratio", type=float, default=0.70)
    parser.add_argument("--chop-lock-rearm-vwap-band-atr-d", type=float, default=0.0)
    parser.add_argument("--anti-choptop-enable", action="store_true")
    parser.add_argument("--anti-choptop-apply-to", choices=["all", "drift"], default="drift")
    parser.add_argument("--anti-choptop-window-start", default="09:45")
    parser.add_argument("--anti-choptop-window-end", default="12:30")
    parser.add_argument("--anti-choptop-rank-high", type=float, default=0.93)
    parser.add_argument("--anti-choptop-rank-low", type=float, default=0.07)
    parser.add_argument("--anti-choptop-sign-flip-5m-min", type=int, default=2)
    parser.add_argument("--anti-choptop-dev-atr-d-min", type=float, default=1.10)
    parser.add_argument("--anti-choptop-ttl-min", type=int, default=3)
    parser.add_argument("--anti-chase-chop-enable", action="store_true")
    parser.add_argument("--anti-chase-window-start", default="09:45")
    parser.add_argument("--anti-chase-window-end", default="14:00")
    parser.add_argument("--anti-chase-apply-to", choices=["all", "fast", "drift"], default="all")
    parser.add_argument("--anti-chase-rank15-min", type=float, default=0.93)
    parser.add_argument("--anti-chase-dist-vwap-atr-d-min", type=float, default=0.12)
    parser.add_argument("--anti-chase-fast-rank15-min", type=float, default=None)
    parser.add_argument("--anti-chase-fast-dist-vwap-atr-d-min", type=float, default=None)
    parser.add_argument("--anti-chase-drift-rank15-min", type=float, default=None)
    parser.add_argument("--anti-chase-drift-dist-vwap-atr-d-min", type=float, default=None)
    parser.add_argument("--anti-chase-signal-ttl-sec", type=int, default=60)
    parser.add_argument("--chop-cross-count-min", type=int, default=4)
    parser.add_argument("--chop-vwap-dist-atr-d-max", type=float, default=0.8)
    parser.add_argument("--chop-drift-5m-atr-d-max", type=float, default=0.9)
    parser.add_argument("--chop-flip-5m-min", type=int, default=3)
    parser.add_argument("--report-file", default="")
    parser.add_argument("--csv-file", default="")
    args = parser.parse_args()
    delayed_confirm_end = _parse_hhmm(args.delayed_confirm_end)
    long_drift_second_confirm_window_start = _parse_hhmm(args.long_drift_second_confirm_window_start)
    long_drift_second_confirm_window_end = _parse_hhmm(args.long_drift_second_confirm_window_end)
    fast_reversal_v3_window_start = _parse_hhmm(args.fast_reversal_v3_window_start)
    fast_reversal_v3_window_end = _parse_hhmm(args.fast_reversal_v3_window_end)
    early_override_window_start = _parse_hhmm(args.early_override_window_start)
    early_override_window_end = _parse_hhmm(args.early_override_window_end)
    low_atr_release_window_start = _parse_hhmm(args.low_atr_release_window_start)
    low_atr_release_window_end = _parse_hhmm(args.low_atr_release_window_end)
    chop_lock_window_start = _parse_hhmm(args.chop_lock_window_start)
    chop_lock_window_end = _parse_hhmm(args.chop_lock_window_end)
    anti_choptop_window_start = _parse_hhmm(args.anti_choptop_window_start)
    anti_choptop_window_end = _parse_hhmm(args.anti_choptop_window_end)
    anti_chase_window_start = _parse_hhmm(args.anti_chase_window_start)
    anti_chase_window_end = _parse_hhmm(args.anti_chase_window_end)
    pre_second_chop_window_start = _parse_hhmm(args.pre_second_chop_window_start)
    pre_second_chop_window_end = _parse_hhmm(args.pre_second_chop_window_end)

    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)
    days = iter_calendar_days(start, end)
    atr_seed_map = load_atr_seed_map(args.atr_seed_path)
    atr_d_by_day, atr_rank_by_day = load_daily_atr_context(args.start_date, args.end_date)
    dynamic_vol_mode = str(args.early_breakout_vol_min_mode).lower() == "dynamic"
    opening_vol_surge_by_day: dict[str, float] = {}
    if dynamic_vol_mode:
        for d in days:
            d_obj = _parse_date(d)
            if d_obj.weekday() >= 5:
                continue
            v = _opening_vol_surge_0938(d)
            if v is not None and v > 0:
                opening_vol_surge_by_day[d] = float(v)
    report_file = args.report_file or str(REPO_ROOT / f"junk/backtest_results/latest/new_filter_daily_report_{args.start_date}_{args.end_date}.md")
    csv_file = args.csv_file or str(REPO_ROOT / f"junk/backtest_results/latest/new_filter_daily_report_{args.start_date}_{args.end_date}.csv")

    print("=" * 100)
    print("v47_fast_drift aligned backtest")
    print(f"date_range={args.start_date}..{args.end_date}, calendar_days={len(days)}")
    print(
        f"notional={args.notional_usd:.0f}, target={args.target_atr:.2f}ATR, "
        f"tp2_trend={args.tp2_trend_atr:.2f}ATR, "
        f"hard_stop={args.hard_stop_atr:.2f}ATR, stop_min_abs={args.stop_min_abs:.2f}, logic_retreat={args.logic_retreat_min}m, "
        f"retreat_buffer={args.logic_retreat_buffer_atr:.2f}ATR, "
        f"day_stop_n={args.day_stop_after_hard_stops}, day_stop_chop_only={bool(args.day_stop_after_hard_stops_chop_only)}, "
        f"stop_cd={args.stop_reentry_cooldown_min:.1f}m, "
        f"pre2_guard={bool(args.pre_second_chop_guard_enable)}({args.pre_second_chop_action})"
    )
    print("openclose_base(backtest)=QQQ 09:30 1m open; openclose_base(live)=price_base(09:30:00-09:30:10, 1s VWAP-like)")
    print("logic_retreat=QQQ 1m close vs QQQ VWAP (decision), execute on TQQQ/SQQQ")
    print(f"atr_cap_mult={args.atr_cap_mult:.2f}, atr_seed_path={args.atr_seed_path}")
    print("=" * 100)

    trading_days = [d for d in days if _parse_date(d).weekday() < 5]
    day_pos = {d: i for i, d in enumerate(trading_days)}

    def _dynamic_early_vol_min_for_day(day: str) -> float:
        fixed = float(args.early_breakout_vol_min)
        if not dynamic_vol_mode:
            return fixed
        idx = day_pos.get(day)
        if idx is None or idx <= 0:
            return fixed
        lookback = max(1, int(args.early_breakout_vol_lookback))
        hist_days = trading_days[max(0, idx - lookback):idx]
        hist_vals = [opening_vol_surge_by_day[d] for d in hist_days if d in opening_vol_surge_by_day]
        qv = _quantile_linear(hist_vals, float(args.early_breakout_vol_quantile))
        if qv is None:
            return fixed
        lo = float(args.early_breakout_vol_min_clamp)
        hi = float(args.early_breakout_vol_max_clamp)
        if hi < lo:
            lo, hi = hi, lo
        return float(min(hi, max(lo, float(qv))))

    def run_day_once(
        day: str,
        min_entry_time: Optional[datetime],
        day_realized_pnl_before: float,
        last_stop_exit_at: Optional[datetime],
        last_stop_exit_side: Optional[str],
        day_hard_stop_count_before: int,
        trade_seq: int,
        first_trade_was_hard_stop: bool,
    ) -> DayOutcome:
        early_vol_min_today = _dynamic_early_vol_min_for_day(day)
        return simulate_day(
            day,
            notional_usd=args.notional_usd,
            target_atr_mult=args.target_atr,
            tp2_trend_atr_mult=args.tp2_trend_atr,
            hard_stop_atr_mult=args.hard_stop_atr,
            stop_min_abs=args.stop_min_abs,
            logic_retreat_minutes=args.logic_retreat_min,
            logic_retreat_buffer_atr=args.logic_retreat_buffer_atr,
            atr_cap_mult=args.atr_cap_mult,
            momentum_deadzone=args.momentum_deadzone,
            fast_v3m_atr_min=args.fast_v3m_atr_min,
            fast_vol_min=args.fast_vol_min,
            drift_bar_min_atr=args.drift_bar_min_atr,
            drift_cum_atr_min=args.drift_cum_atr_min,
            fast_crosses_threshold=args.fast_crosses_threshold,
            fast_cooldown_min=args.fast_cooldown_min,
            veto_rebound_live_atr=args.veto_rebound_live_atr,
            atr_seed_map=atr_seed_map,
            atr_d=atr_d_by_day.get(day),
            atr_rank=float(atr_rank_by_day.get(day, 0.5)),
            atr_rank_chop_max=args.atr_rank_chop_max,
            early_breakout_move_atr=args.early_breakout_move_atr,
            early_breakout_vol_min=early_vol_min_today,
            chop_mode=args.chop_mode,
            mr_tp_atr=args.mr_tp_atr,
            mr_sl_atr=args.mr_sl_atr,
            mr_max_hold_min=args.mr_max_hold_min,
            a_day_size_mult=args.a_day_size_mult,
            a_day_atr_rank_min=args.a_day_atr_rank_min,
            drift_cum_atrd_min=args.drift_cum_atrd_min,
            early_lock_mode=args.early_lock_mode,
            delayed_confirm_move5m_min=args.delayed_confirm_move5m_min,
            delayed_confirm_end=delayed_confirm_end,
            delayed_confirm_adaptive_enable=args.delayed_confirm_adaptive_enable,
            delayed_confirm_adaptive_move5m_min=args.delayed_confirm_adaptive_move5m_min,
            delayed_confirm_adaptive_cross_count_max=args.delayed_confirm_adaptive_cross_count_max,
            delayed_confirm_adaptive_vwap_dev_atr_d_min=args.delayed_confirm_adaptive_vwap_dev_atr_d_min,
            min_entry_time=min_entry_time,
            day_realized_pnl_before=day_realized_pnl_before,
            entry_confirm_delay_min=args.entry_confirm_delay_min,
            long_drift_second_confirm=args.long_drift_second_confirm,
            long_drift_second_confirm_side=args.long_drift_second_confirm_side,
            long_drift_second_confirm_window_start=long_drift_second_confirm_window_start,
            long_drift_second_confirm_window_end=long_drift_second_confirm_window_end,
            long_drift_second_confirm_delay_min=args.long_drift_second_confirm_delay_min,
            breakout_followthrough_confirm=args.breakout_followthrough_confirm,
            skip_short_on_narrow_day=args.skip_short_on_narrow_day,
            narrow_open_range_atr_max=args.narrow_open_range_atr_max,
            first_trade_block_0945_0950=args.first_trade_block_0945_0950,
            first_trade_second_test_require_breakout=args.first_trade_second_test_require_breakout,
            first_trade_skip_failed_breakout_pullback=args.first_trade_skip_failed_breakout_pullback,
            entry_dist_0940_max_atr_d=args.entry_dist_0940_max_atr_d,
            fast_reversal_guard=args.fast_reversal_guard,
            fast_reversal_v3_only_first_trade=args.fast_reversal_v3_only_first_trade,
            fast_reversal_v3_window_start=fast_reversal_v3_window_start,
            fast_reversal_v3_window_end=fast_reversal_v3_window_end,
            fast_reversal_v3_max_entry_dist_atr_d=args.fast_reversal_v3_max_entry_dist_atr_d,
            early_override_direct_entry=args.early_override_direct_entry,
            early_override_window_start=early_override_window_start,
            early_override_window_end=early_override_window_end,
            early_override_max_counter_bars=args.early_override_max_counter_bars,
            early_override_last3_confirm=args.early_override_last3_confirm,
            early_override_min_vwap_dist_atr_d=args.early_override_min_vwap_dist_atr_d,
            early_override_max_cross_count=args.early_override_max_cross_count,
            early_override_disable_add=args.early_override_disable_add,
            early_quick_exit_min=args.early_quick_exit_min,
            early_continuation_min_atr_d=args.early_continuation_min_atr_d,
            low_atr_release_enable=args.low_atr_release_enable,
            low_atr_release_window_start=low_atr_release_window_start,
            low_atr_release_window_end=low_atr_release_window_end,
            low_atr_release_max_cross_count=args.low_atr_release_max_cross_count,
            low_atr_release_min_vwap_dist_atr_d=args.low_atr_release_min_vwap_dist_atr_d,
            low_atr_release_drift_bar_min_atr=args.low_atr_release_drift_bar_min_atr,
            low_atr_release_drift_cum_atr_min=args.low_atr_release_drift_cum_atr_min,
            low_atr_release_quick_exit_min=args.low_atr_release_quick_exit_min,
            low_atr_release_continuation_min_atr_d=args.low_atr_release_continuation_min_atr_d,
            chop_lock_enable=args.chop_lock_enable,
            chop_lock_window_start=chop_lock_window_start,
            chop_lock_window_end=chop_lock_window_end,
            chop_lock_cross_count_15m_min=args.chop_lock_cross_count_15m_min,
            chop_lock_require_day_pnl_nonpositive=args.chop_lock_require_day_pnl_nonpositive,
            chop_lock_disable_add=args.chop_lock_disable_add,
            chop_lock_probe_ratio=args.chop_lock_probe_ratio,
            chop_lock_rearm_vwap_band_atr_d=args.chop_lock_rearm_vwap_band_atr_d,
            anti_choptop_enable=args.anti_choptop_enable,
            anti_choptop_apply_to=args.anti_choptop_apply_to,
            anti_choptop_window_start=anti_choptop_window_start,
            anti_choptop_window_end=anti_choptop_window_end,
            anti_choptop_rank_high=args.anti_choptop_rank_high,
            anti_choptop_rank_low=args.anti_choptop_rank_low,
            anti_choptop_sign_flip_5m_min=args.anti_choptop_sign_flip_5m_min,
            anti_choptop_dev_atr_d_min=args.anti_choptop_dev_atr_d_min,
            anti_choptop_ttl_min=args.anti_choptop_ttl_min,
            anti_chase_chop_enable=args.anti_chase_chop_enable,
            anti_chase_window_start=anti_chase_window_start,
            anti_chase_window_end=anti_chase_window_end,
            anti_chase_apply_to=args.anti_chase_apply_to,
            anti_chase_rank15_min=args.anti_chase_rank15_min,
            anti_chase_dist_vwap_atr_d_min=args.anti_chase_dist_vwap_atr_d_min,
            anti_chase_fast_rank15_min=args.anti_chase_fast_rank15_min,
            anti_chase_fast_dist_vwap_atr_d_min=args.anti_chase_fast_dist_vwap_atr_d_min,
            anti_chase_drift_rank15_min=args.anti_chase_drift_rank15_min,
            anti_chase_drift_dist_vwap_atr_d_min=args.anti_chase_drift_dist_vwap_atr_d_min,
            anti_chase_signal_ttl_sec=args.anti_chase_signal_ttl_sec,
            chop_cross_count_min=args.chop_cross_count_min,
            chop_vwap_dist_atr_d_max=args.chop_vwap_dist_atr_d_max,
            chop_drift_5m_atr_d_max=args.chop_drift_5m_atr_d_max,
            chop_flip_5m_min=args.chop_flip_5m_min,
            stop_reentry_cooldown_min=args.stop_reentry_cooldown_min,
            last_stop_exit_at=last_stop_exit_at,
            last_stop_exit_side=last_stop_exit_side,
            day_hard_stop_count_before=day_hard_stop_count_before,
            day_stop_after_hard_stops=args.day_stop_after_hard_stops,
            day_stop_after_hard_stops_chop_only=args.day_stop_after_hard_stops_chop_only,
            trade_seq=trade_seq,
            first_trade_was_hard_stop=first_trade_was_hard_stop,
            pre_second_chop_guard_enable=args.pre_second_chop_guard_enable,
            pre_second_chop_window_start=pre_second_chop_window_start,
            pre_second_chop_window_end=pre_second_chop_window_end,
            pre_second_chop_cross_count_15m_min=args.pre_second_chop_cross_count_15m_min,
            pre_second_chop_require_first_stop=args.pre_second_chop_require_first_stop,
            pre_second_chop_action=args.pre_second_chop_action,
            pre_second_chop_probe_ratio=args.pre_second_chop_probe_ratio,
        )

    results: list[DayOutcome] = []
    extra_trades_by_day: dict[str, list[Trade]] = defaultdict(list)
    for d in days:
        d_obj = _parse_date(d)
        if d_obj.weekday() >= 5:
            results.append(DayOutcome(day=d, trade=None, no_trade_reason="weekend_market_closed", reject_counts={}))
            continue
        outcome = run_day_once(
            d,
            min_entry_time=None,
            day_realized_pnl_before=0.0,
            last_stop_exit_at=None,
            last_stop_exit_side=None,
            day_hard_stop_count_before=0,
            trade_seq=1,
            first_trade_was_hard_stop=False,
        )
        if outcome.trade is not None and int(args.max_trades_per_day) > 1:
            if outcome.trade.entry_mode == "LOW_ATR_RELEASE":
                # Release entries are probe-like by design: one shot only.
                results.append(outcome)
                continue
            prev_trade = outcome.trade
            for _ in range(max(0, int(args.max_trades_per_day) - 1)):
                current_trade_count = 1 + len(extra_trades_by_day[d])
                next_trade_seq = current_trade_count + 1
                day_trades_so_far = [outcome.trade] + list(extra_trades_by_day[d])
                hard_stops = sum(1 for t in day_trades_so_far if (t.exit_reason or "") == "hard_stop_hit")

                # Day-level circuit breaker: halt after too many hard-stop exits.
                if args.day_stop_after_hard_stops is not None and int(args.day_stop_after_hard_stops) > 0:
                    # For chop-only mode, do not pre-break here; let simulate_day decide
                    # with real-time chop state at each candidate timestamp.
                    if (not bool(args.day_stop_after_hard_stops_chop_only)) and hard_stops >= int(args.day_stop_after_hard_stops):
                        break

                # Day-level circuit breaker: halt after N consecutive losses.
                if args.day_stop_after_consecutive_losses is not None and int(args.day_stop_after_consecutive_losses) > 0:
                    consec_losses = 0
                    for t in reversed(day_trades_so_far):
                        if float(t.exec_pnl or 0.0) < 0:
                            consec_losses += 1
                        else:
                            break
                    if consec_losses >= int(args.day_stop_after_consecutive_losses):
                        break

                # If first trade is a loss, allow at most one extra trade (max 2 total).
                if args.first_loss_allow_only_one_extra and float(outcome.trade.exec_pnl or 0.0) < 0 and next_trade_seq >= 3:
                    break

                if next_trade_seq == 3 and args.third_trade_require_first2_positive:
                    if len(extra_trades_by_day[d]) < 1:
                        break
                    first_pnl = float(outcome.trade.exec_pnl or 0.0)
                    second_pnl = float(extra_trades_by_day[d][0].exec_pnl or 0.0)
                    if (first_pnl + second_pnl) <= 0.0:
                        break
                if args.second_trade_first_min_r is not None:
                    risk_r_usd = (
                        float(args.hard_stop_atr)
                        * float(prev_trade.atr_entry_used or 0.0)
                        * float(prev_trade.qty or 0.0)
                    )
                    if risk_r_usd <= 0:
                        break
                    first_r = float(prev_trade.exec_pnl or 0.0) / risk_r_usd
                    if first_r <= float(args.second_trade_first_min_r):
                        break
                elif args.second_trade_require_first_win and float(prev_trade.exec_pnl or 0.0) <= 0:
                    break
                next_min_entry = (
                    prev_trade.exit_time + timedelta(minutes=1)
                    if prev_trade.exit_time is not None
                    else prev_trade.entry_time + timedelta(minutes=1)
                )
                day_realized_pnl_before = float(sum(float(t.exec_pnl or 0.0) for t in day_trades_so_far))
                last_stop_exit_at: Optional[datetime] = None
                last_stop_exit_side: Optional[str] = None
                for t in reversed(day_trades_so_far):
                    if (t.exit_reason or "") == "hard_stop_hit" and t.exit_time is not None:
                        last_stop_exit_at = t.exit_time
                        last_stop_exit_side = t.entry_direction
                        break

                extra = run_day_once(
                    d,
                    min_entry_time=next_min_entry,
                    day_realized_pnl_before=day_realized_pnl_before,
                    last_stop_exit_at=last_stop_exit_at,
                    last_stop_exit_side=last_stop_exit_side,
                    day_hard_stop_count_before=hard_stops,
                    trade_seq=next_trade_seq,
                    first_trade_was_hard_stop=((outcome.trade.exit_reason or "") == "hard_stop_hit"),
                )
                if extra.trade is None:
                    break
                extra_trades_by_day[d].append(extra.trade)
                prev_trade = extra.trade
        results.append(outcome)

    total = 0.0
    wins = 0
    losses = 0
    flats = 0
    monthly = defaultdict(
        lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "target": 0, "tp2": 0, "stop": 0, "trail": 0, "retreat": 0, "eod": 0}
    )
    no_trade_reasons = defaultdict(int)

    for outcome in results:
        d = outcome.day
        day_trades: list[Trade] = []
        if outcome.trade is not None:
            day_trades.append(outcome.trade)
        day_trades.extend(extra_trades_by_day.get(d, []))
        if not day_trades:
            no_trade_reasons[outcome.no_trade_reason] += 1
            continue

        for t in day_trades:
            pnl = float(t.exec_pnl or 0.0)
            total += pnl
            m = d[:7]
            monthly[m]["pnl"] += pnl
            monthly[m]["trades"] += 1
            if pnl > 0:
                wins += 1
                monthly[m]["wins"] += 1
            elif pnl < 0:
                losses += 1
                monthly[m]["losses"] += 1
            else:
                flats += 1

            r = t.exit_reason or ""
            if "tp1_then_tp2_trend_hit" in r:
                monthly[m]["target"] += 1
                monthly[m]["tp2"] += 1
            elif "tp1_then_trailing_stop" in r:
                monthly[m]["target"] += 1
                monthly[m]["trail"] += 1
            elif "tp1_then_eod" in r:
                monthly[m]["target"] += 1
                monthly[m]["eod"] += 1
            elif r == "mr_tp":
                monthly[m]["target"] += 1
            elif r == "mr_sl":
                monthly[m]["stop"] += 1
            elif r == "mr_time_exit":
                monthly[m]["retreat"] += 1
            elif "target" in r:
                monthly[m]["target"] += 1
            elif "hard_stop" in r:
                monthly[m]["stop"] += 1
            elif "retreat" in r:
                monthly[m]["retreat"] += 1
            else:
                monthly[m]["eod"] += 1

    traded_days = sum(1 for x in results if (x.trade is not None or len(extra_trades_by_day.get(x.day, [])) > 0))
    trade_count = wins + losses + flats
    print(f"trades={trade_count}, wins={wins}, losses={losses}, flat={flats}, win_rate={(wins / max(1, trade_count)) * 100:.1f}%")
    print(f"no_trade_days={len(results) - traded_days}")
    print(f"total_pnl=${total:+,.2f}")
    print("-" * 100)
    print(
        f"{'Month':<10} {'Trades':>6} {'WinRate':>8} {'PnL':>14} {'Target':>7} "
        f"{'TP2':>6} {'Stop':>6} {'Trail':>7} {'Retreat':>8} {'EOD':>5}"
    )
    print("-" * 108)

    for m in sorted(monthly.keys()):
        s = monthly[m]
        wr = (s["wins"] / max(1, s["trades"])) * 100
        print(
            f"{m:<10} {s['trades']:>6} {wr:>7.1f}% {s['pnl']:>+14,.2f} {s['target']:>7} "
            f"{s['tp2']:>6} {s['stop']:>6} {s['trail']:>7} {s['retreat']:>8} {s['eod']:>5}"
        )

    if no_trade_reasons:
        print("-" * 100)
        print("top_no_trade_reasons")
        for k, v in sorted(no_trade_reasons.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"{k}: {v}")

    report_lines: list[str] = []
    report_lines.append("# 回测完整日报")
    report_lines.append("")
    report_lines.append(f"- 区间: `{args.start_date}` 到 `{args.end_date}`（自然日 {len(results)} 天）")
    report_lines.append(
        f"- 参数: `tp1={args.target_atr:.1f} ATR`，`tp2_trend={args.tp2_trend_atr:.1f} ATR`，`hard_stop={args.hard_stop_atr:.1f} ATR`，`stop_min_abs={args.stop_min_abs:.2f}`，`logic_retreat={args.logic_retreat_min}m`，"
        f" `retreat_buffer={args.logic_retreat_buffer_atr:.1f} ATR`，`notional={args.notional_usd:.0f}`，"
        f" `a_day_size_mult={args.a_day_size_mult:.2f}`，`a_day_rank_min={args.a_day_atr_rank_min:.2f}`，"
        f" `max_trades_per_day={args.max_trades_per_day}`，`day_stop_after_hard_stops={args.day_stop_after_hard_stops}`，"
        f" `day_stop_after_hard_stops_chop_only={bool(args.day_stop_after_hard_stops_chop_only)}`，`stop_reentry_cooldown_min={args.stop_reentry_cooldown_min:.1f}`，"
        f" `pre_second_chop_guard_enable={bool(args.pre_second_chop_guard_enable)}`，`pre_second_chop_action={args.pre_second_chop_action}`，"
        f" `pre_second_chop_cross_count_15m_min={args.pre_second_chop_cross_count_15m_min}`，`pre_second_chop_probe_ratio={args.pre_second_chop_probe_ratio:.2f}`"
    )
    report_lines.append(f"- 总交易: `{trade_count}`（胜 `{wins}` / 负 `{losses}` / 平 `{flats}`），胜率: `{(wins / max(1, trade_count)) * 100:.1f}%`，总盈亏: `${total:+,.2f}`")
    report_lines.append("")
    report_lines.append("## 月度汇总")
    report_lines.append("")
    report_lines.append("| 月份 | 交易笔数 | 胜率 | 总盈亏 | Target(含TP1) | TP2命中 | HardStop | TrailExit | LogicRetreat | EOD |")
    report_lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m in sorted(monthly.keys()):
        s = monthly[m]
        wr = (s["wins"] / max(1, s["trades"])) * 100
        report_lines.append(
            f"| {m} | {s['trades']} | {wr:.1f}% | {s['pnl']:+,.2f} | {s['target']} | "
            f"{s['tp2']} | {s['stop']} | {s['trail']} | {s['retreat']} | {s['eod']} |"
        )

    report_lines.append("")
    report_lines.append("## 每日明细")
    report_lines.append("")
    report_lines.append("| 日期 | 成交状态 | 未成交原因/出场原因 | 入场(ET) | 出场(ET) | 成交金额 | 止盈金额(命中时) | 当日盈亏 |")
    report_lines.append("|---|---|---|---|---|---:|---:|---:|")
    for outcome in results:
        day_trades: list[Trade] = []
        if outcome.trade is not None:
            day_trades.append(outcome.trade)
        day_trades.extend(extra_trades_by_day.get(outcome.day, []))
        if not day_trades:
            report_lines.append(
                f"| {outcome.day} | 未成交 | {outcome.no_trade_reason} | - | - | 0 | 0 | 0 |"
            )
            continue
        for idx, t in enumerate(day_trades, start=1):
            entry_desc = f"{t.entry_time.strftime('%H:%M')} {t.entry_mode}/{t.entry_direction} {t.exec_symbol}@{t.entry_px:.2f}"
            exit_desc = f"{t.exit_time.strftime('%H:%M') if t.exit_time else '-'} {t.exit_reason or '-'} @{(t.exit_px if t.exit_px is not None else 0):.2f}"
            reason = (t.exit_reason or "-") + (f" (#{idx})" if len(day_trades) > 1 else "")
            report_lines.append(
                f"| {outcome.day} | 成交 | {reason} | {entry_desc} | {exit_desc} | {t.notional:,.2f} | {(t.target_pnl_if_hit or 0):,.2f} | {(t.exec_pnl or 0):+,.2f} |"
            )

    Path(report_file).parent.mkdir(parents=True, exist_ok=True)
    Path(report_file).write_text("\n".join(report_lines), encoding="utf-8")

    def _fmt_et_csv(ts: Optional[datetime]) -> str:
        if ts is None:
            return ""
        ts_et = ts.astimezone(ET) if ts.tzinfo is not None else ts.replace(tzinfo=ET)
        # Export as ET-local naive timestamp text to prevent spreadsheet auto timezone conversion.
        return ts_et.strftime("%Y-%m-%d %H:%M:%S")

    csv_lines = [
        "date,status,reason,entry_time,entry_mode,entry_direction,exec_symbol,entry_px,exit_time,exit_reason,exit_px,notional,target_pnl_if_hit,exec_pnl,minutes_held,mfe_5m,mae_5m,fast_reversal_guard_hit,day_realized_pnl_before,chop_lock_on_entry,no_trade_reason"
    ]
    for outcome in results:
        day_trades: list[Trade] = []
        if outcome.trade is not None:
            day_trades.append(outcome.trade)
        day_trades.extend(extra_trades_by_day.get(outcome.day, []))
        if not day_trades:
            csv_lines.append(f"{outcome.day},NO_TRADE,{outcome.no_trade_reason},,,,,,,,,0,0,0,0,0,0,,0.00,0,{outcome.no_trade_reason}")
            continue
        for t in day_trades:
            csv_lines.append(
                ",".join(
                    [
                        outcome.day,
                        "TRADE",
                        (t.exit_reason or ""),
                        _fmt_et_csv(t.entry_time),
                        t.entry_mode,
                        t.entry_direction,
                        t.exec_symbol,
                        f"{t.entry_px:.4f}",
                        _fmt_et_csv(t.exit_time),
                        t.exit_reason or "",
                        f"{(t.exit_px or 0):.4f}",
                        f"{t.notional:.2f}",
                        f"{(t.target_pnl_if_hit or 0):.2f}",
                        f"{(t.exec_pnl or 0):.2f}",
                        str(t.minutes_held or 0),
                        f"{t.mfe_5m:.4f}",
                        f"{t.mae_5m:.4f}",
                        "1" if t.fast_reversal_guard_hit else "0",
                        f"{float(t.day_realized_pnl_before):.2f}",
                        "1" if t.chop_lock_on_entry else "0",
                        "",
                    ]
                )
            )
    Path(csv_file).parent.mkdir(parents=True, exist_ok=True)
    Path(csv_file).write_text("\n".join(csv_lines), encoding="utf-8")
    print(f"report_file={report_file}")
    print(f"csv_file={csv_file}")


if __name__ == "__main__":
    main()
