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
class DayResult:
    day: str
    data_source: str
    traded: bool
    mode: str
    direction: str
    entry_ts: str
    entry_px: float
    entry_dev_atr: float
    blocks_top: list[tuple[str, int]]


def _minute_floor(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _atr_1m(closes: list[float], highs: list[float], lows: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: list[float] = []
    for i in range(len(closes) - period, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(float(tr))
    return float(sum(trs) / len(trs)) if trs else None


def _load_atr_seed(path: Path, day: str) -> float:
    if not path.exists():
        return 0.0
    raw = json.loads(path.read_text(encoding="utf-8"))
    rec = (raw.get("days") or {}).get(day) or raw.get(day) or {}
    if isinstance(rec, dict):
        val = rec.get("TQQQ")
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict) and isinstance(val.get("atr_pre"), (int, float)):
            return float(val.get("atr_pre"))
    latest = raw.get("latest") or {}
    if str(latest.get("day")) == day:
        atr_pre = latest.get("atr_pre") or {}
        if isinstance(atr_pre, dict) and isinstance(atr_pre.get("TQQQ"), (int, float)):
            return float(atr_pre["TQQQ"])
    return 0.0


async def _load_ticks_sfs(day: str, n: int) -> dict[str, list[Tick]]:
    reader = SFS1sReader()
    symbols = ["QQQ", "QQEW"]
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
    symbols = ["QQQ", "QQEW"]
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


def _load_ticks_1m_clickhouse(day: str) -> dict[str, list[Tick]]:
    symbols = ["QQQ", "QQEW"]
    out: dict[str, list[Tick]] = {s: [] for s in symbols}
    client = get_client()
    for sym in symbols:
        rows = client.query(
            """
            SELECT ts_start, close, volume
            FROM bars_1m_ibkr
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
    if out.get("QQQ"):
        return out, "sfs_1s"
    out_ch = _load_ticks_5s_clickhouse(day)
    if out_ch.get("QQQ"):
        return out_ch, "clickhouse_5s"
    out_1m = _load_ticks_1m_clickhouse(day)
    return out_1m, "clickhouse_1m"


def run_day(
    day: str,
    ticks: dict[str, list[Tick]],
    data_source: str,
    atr_seed_tqqq: float,
    *,
    ab_time_start: time = time(9, 32),
    ab_time_end: time = time(10, 30),
    b_time_start: time = time(9, 32),
    b_time_end: time = time(9, 50),
    qqq_max_age_sec: float = 15.0,
    qqq_trade_allow_age_sec: float = 10.0,
    a_overext_mult: float = 1.60,
    a_reentry_window_sec: float = 90.0,
    a_pullback_mult_open: float = 1.50,
    a_pullback_mult_late: float = 1.40,
    a_pullback_switch_time: time = time(10, 0),
    a_latest_entry_time: time = time(15, 30),
    a_entry_max_dev_atr: float = 999.0,
    a_armed_ttl_sec: float = 99999.0,
    a_recent_runup_sec: int = 90,
    a_recent_runup_max_live_atr: float = 999.0,
    a_runup_guard_sec: int = 0,
    a_runup_guard_live_atr: float = 1.5,
    a_runup_pullback_live_atr: float = 0.35,
    b_decay_limit: float = 0.95,
    a_decay_limit: float = 0.95,
    b_decay_lookback_sec: int = 3,
    b_vol_normal: float = 1.35,
    b_vol_fast: float = 1.25,
    b_require_two_sign: bool = False,
    b_require_prev_vol: bool = False,
    b_prev_vol_min: float = 1.2,
    b_ext_base_mult: float = 1.6,
    b_enable_burst_ext: bool = True,
    a_vol_min: float = 0.0,
    anti_decay_limit: float = 0.0,
    anti_decay_lookback_sec: int = 0,
    short_exhaustion_enable: bool = False,
    short_exhaustion_window_sec: int = 480,
    short_exhaustion_drop_live_atr: float = 1.8,
    short_exhaustion_near_low_live_atr: float = 0.35,
    short_exhaustion_bounce_sec: int = 20,
    short_exhaustion_bounce_live_atr: float = 0.2,
    short_exhaustion_cooldown_sec: int = 180,
    reversal_enable: bool = False,
    reversal_slope_sec: int = 30,
    reversal_slope_live_atr: float = 0.15,
    reversal_vol_min: float = 1.2,
    reversal_require_two_positive: bool = True,
    entry_long_runup_sec: int = 0,
    entry_long_runup_max_live_atr: float = 999.0,
    regime_exhaustion_enable: bool = False,
    regime_window_sec: int = 480,
    regime_runup_live_atr: float = 2.0,
    regime_near_extreme_live_atr: float = 0.25,
    regime_stall_sec: int = 20,
    regime_stall_live_atr: float = 0.05,
    regime_cooldown_sec: int = 900,
    regime_pullback_live_atr: float = 1.0,
    regime_rebound_sec: int = 30,
    regime_rebound_live_atr: float = 0.25,
    regime_reversal_vol_min: float = 1.2,
    ab_volatile_day_skip_enable: bool = False,
    ab_volatile_check_end: time = time(10, 5),
    ab_volatile_range_live_atr_min: float = 4.5,
    ab_volatile_directionality_live_atr_min: float = 1.0,
    ab_volatile_reversal_washout_live_atr_min: float = 1.5,
    ab_volatile_reversal_rebound_live_atr_min: float = 0.8,
    ab_volatile_allow_early_reversal: bool = True,
    ab_volatile_reversal_vol_min: float = 1.2,
    ab_volatile_reversal_require_two_positive: bool = True,
    ab_volatile_probe_enable: bool = True,
    ab_volatile_probe_break_live_atr: float = 0.6,
    ab_volatile_probe_reclaim_live_atr: float = 0.2,
    ab_volatile_disable_on_coarse_data: bool = True,
    atr_cap_mult: float = 1.8,
) -> DayResult:
    qqq = ticks.get("QQQ") or []
    if not qqq:
        return DayResult(day=day, data_source=data_source, traded=False, mode="", direction="", entry_ts="", entry_px=0.0, entry_dev_atr=0.0, blocks_top=[("no_data", 1)])

    blocks: Counter[str] = Counter()
    q_hist: list[Tick] = []
    q_pv = 0.0
    q_v = 0.0
    v3m_abs_hist: deque[float] = deque(maxlen=20)
    prev_vol_ratio: Optional[float] = None

    m_high: dict[datetime, float] = {}
    m_low: dict[datetime, float] = {}
    m_close: dict[datetime, float] = {}
    m_vol: dict[datetime, float] = {}

    a_state = "WAIT_MARK"
    a_mark_ts: Optional[datetime] = None
    a_mark_direction: Optional[str] = None
    a_armed_ts: Optional[datetime] = None
    short_veto_until: Optional[datetime] = None
    prev_v3m_signed: Optional[float] = None
    regime_state: Optional[str] = None  # WAIT_LONG_RESET | WAIT_SHORT_RESET
    regime_until: Optional[datetime] = None
    regime_anchor_high: Optional[float] = None
    regime_anchor_low: Optional[float] = None
    ab_volatile_veto_direction: Optional[str] = None
    ab_volatile_anchor_px: Optional[float] = None
    ab_volatile_extreme_px: Optional[float] = None
    ab_volatile_probe_anchor_px: Optional[float] = None
    ab_volatile_probe_extreme_px: Optional[float] = None
    ab_volatile_probe_armed = False

    for q in qqq:
        ts = q.ts
        if ts.time() < ab_time_start or ts.time() > ab_time_end:
            continue

        age_qqq_sec = 0.0
        if age_qqq_sec > qqq_max_age_sec:
            blocks["data_not_ready_qqq"] += 1
            continue

        q_hist.append(q)
        q_pv += q.close * max(0.0, q.volume)
        q_v += max(0.0, q.volume)
        if q_v <= 0:
            blocks["data_not_ready_qqq"] += 1
            continue
        q_vwap = q_pv / q_v

        m = _minute_floor(ts)
        if m not in m_high:
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
        if atr_live is None or atr_seed_tqqq <= 0:
            blocks["atr_not_ready"] += 1
            continue
        atr_eff = min(float(atr_live), float(atr_cap_mult * atr_seed_tqqq))
        if atr_eff <= 0:
            blocks["atr_not_ready"] += 1
            continue

        base_idx = None
        for i in range(len(q_hist) - 1, -1, -1):
            if (ts - q_hist[i].ts).total_seconds() >= 180:
                base_idx = i
                break
        if base_idx is None:
            blocks["v3m_not_ready"] += 1
            continue
        v3m_signed = float(q.close - q_hist[base_idx].close)
        v3_abs = abs(v3m_signed)
        v3m_abs_hist.append(v3_abs)

        prev_minutes = [k for k in minute_keys if k < m][-10:]
        if len(prev_minutes) < 10:
            blocks["volume_history_insufficient"] += 1
            continue
        vol_ma10 = sum(m_vol[k] for k in prev_minutes) / 10.0
        vol_ratio = float(m_vol[m] / max(1e-9, vol_ma10))

        def _close_at_or_before(ts_ref: datetime) -> Optional[float]:
            for q_prev in reversed(q_hist):
                if q_prev.ts <= ts_ref:
                    return float(q_prev.close)
            return None

        force_direction: Optional[str] = None
        entry_path: Optional[str] = None
        if (
            reversal_enable
            and short_veto_until is not None
            and ts <= short_veto_until
        ):
            slope_ref = _close_at_or_before(ts - timedelta(seconds=max(5, int(reversal_slope_sec))))
            slope_ok = False
            if slope_ref is not None and atr_live and atr_live > 0:
                slope_ok = ((float(q.close) - float(slope_ref)) / max(float(atr_live), 1e-9)) >= float(reversal_slope_live_atr)
            two_pos_ok = True if not reversal_require_two_positive else (prev_v3m_signed is not None and prev_v3m_signed > 0 and v3m_signed > 0)
            if slope_ok and vol_ratio >= float(reversal_vol_min) and two_pos_ok:
                force_direction = "LONG"

        if force_direction is not None:
            direction = force_direction
        elif q.close > q_vwap:
            direction = "LONG"
        elif q.close < q_vwap:
            direction = "SHORT"
        else:
            blocks["direction_mismatch"] += 1
            continue
        dev_abs = abs(q.close - q_vwap)
        dev_atr = dev_abs / max(atr_eff, 1e-9)
        dev_live_atr = dev_abs / max(float(atr_live), 1e-9)
        deadzone = 0.15 * atr_eff
        v3m_min = 0.35 * atr_eff
        v3m_fast = 0.50 * atr_eff

        early_reversal_direction: Optional[str] = None
        volatile_skip_active = bool(ab_volatile_day_skip_enable)
        if ab_volatile_disable_on_coarse_data and data_source in {"clickhouse_1m"}:
            volatile_skip_active = False

        if volatile_skip_active:
            if ab_volatile_veto_direction is not None and direction == ab_volatile_veto_direction:
                if not ab_volatile_allow_early_reversal:
                    blocks["AB_volatile_wrong_side_veto"] += 1
                    prev_vol_ratio = vol_ratio
                    prev_v3m_signed = v3m_signed
                    continue
            if ts.time() > ab_volatile_check_end:
                day_start = ts.replace(hour=9, minute=32, second=0, microsecond=0)
                check_end = ts.replace(
                    hour=ab_volatile_check_end.hour,
                    minute=ab_volatile_check_end.minute,
                    second=ab_volatile_check_end.second,
                    microsecond=0,
                )
                morning_prices = [q_prev.close for q_prev in q_hist if day_start <= q_prev.ts <= check_end]
                if len(morning_prices) >= 30:
                    morning_range_live_atr = (max(morning_prices) - min(morning_prices)) / max(float(atr_live), 1e-9)
                    opening_move_live_atr = abs(float(morning_prices[-1]) - float(morning_prices[0])) / max(float(atr_live), 1e-9)
                    if (
                        morning_range_live_atr >= float(ab_volatile_range_live_atr_min)
                        and opening_move_live_atr >= float(ab_volatile_directionality_live_atr_min)
                    ):
                        ab_volatile_veto_direction = "SHORT" if float(morning_prices[-1]) < float(morning_prices[0]) else "LONG"
                        ab_volatile_anchor_px = float(morning_prices[-1])
                        ab_volatile_extreme_px = float(morning_prices[-1])
                        ab_volatile_probe_anchor_px = None
                        ab_volatile_probe_extreme_px = None
                        ab_volatile_probe_armed = False
                    if ab_volatile_veto_direction is not None and direction == ab_volatile_veto_direction:
                        blocks["AB_volatile_wrong_side_veto"] += 1
                        prev_vol_ratio = vol_ratio
                        prev_v3m_signed = v3m_signed
                        continue
            if ab_volatile_veto_direction is not None and ab_volatile_extreme_px is not None:
                now_px = float(q.close)
                if ab_volatile_veto_direction == "SHORT":
                    ab_volatile_extreme_px = min(float(ab_volatile_extreme_px), now_px)
                else:
                    ab_volatile_extreme_px = max(float(ab_volatile_extreme_px), now_px)
            if (
                ab_volatile_veto_direction is not None
                and direction != ab_volatile_veto_direction
                and ab_volatile_probe_enable
            ):
                now_px = float(q.close)
                if ab_volatile_probe_anchor_px is None:
                    ab_volatile_probe_anchor_px = now_px
                    ab_volatile_probe_extreme_px = now_px
                    ab_volatile_probe_armed = False
                    blocks["AB_volatile_probe_wait"] += 1
                    prev_vol_ratio = vol_ratio
                    prev_v3m_signed = v3m_signed
                    continue
                probe_anchor = float(ab_volatile_probe_anchor_px)
                probe_extreme = float(ab_volatile_probe_extreme_px or now_px)
                if ab_volatile_veto_direction == "SHORT":
                    probe_extreme = min(probe_extreme, now_px)
                    if probe_extreme <= (probe_anchor - float(ab_volatile_probe_break_live_atr) * float(atr_live)):
                        ab_volatile_probe_armed = True
                    ready = ab_volatile_probe_armed and now_px >= (probe_anchor + float(ab_volatile_probe_reclaim_live_atr) * float(atr_live))
                else:
                    probe_extreme = max(probe_extreme, now_px)
                    if probe_extreme >= (probe_anchor + float(ab_volatile_probe_break_live_atr) * float(atr_live)):
                        ab_volatile_probe_armed = True
                    ready = ab_volatile_probe_armed and now_px <= (probe_anchor - float(ab_volatile_probe_reclaim_live_atr) * float(atr_live))
                ab_volatile_probe_extreme_px = probe_extreme
                if not ready:
                    blocks["AB_volatile_probe_wait"] += 1
                    prev_vol_ratio = vol_ratio
                    prev_v3m_signed = v3m_signed
                    continue
            if (
                ab_volatile_veto_direction is not None
                and direction != ab_volatile_veto_direction
                and (not ab_volatile_probe_enable)
                and ab_volatile_anchor_px is not None
                and ab_volatile_extreme_px is not None
            ):
                now_px = float(q.close)
                if ab_volatile_veto_direction == "SHORT":
                    washout_live_atr = (float(ab_volatile_anchor_px) - float(ab_volatile_extreme_px)) / max(float(atr_live), 1e-9)
                    rebound_live_atr = (now_px - float(ab_volatile_extreme_px)) / max(float(atr_live), 1e-9)
                else:
                    washout_live_atr = (float(ab_volatile_extreme_px) - float(ab_volatile_anchor_px)) / max(float(atr_live), 1e-9)
                    rebound_live_atr = (float(ab_volatile_extreme_px) - now_px) / max(float(atr_live), 1e-9)
                if (
                    washout_live_atr < float(ab_volatile_reversal_washout_live_atr_min)
                    or rebound_live_atr < float(ab_volatile_reversal_rebound_live_atr_min)
                ):
                    blocks["AB_volatile_reversal_not_ready"] += 1
                    prev_vol_ratio = vol_ratio
                    prev_v3m_signed = v3m_signed
                    continue
            if (
                ab_volatile_veto_direction is not None
                and direction == ab_volatile_veto_direction
                and ab_volatile_allow_early_reversal
                and ab_volatile_anchor_px is not None
                and ab_volatile_extreme_px is not None
            ):
                if ab_volatile_veto_direction == "SHORT":
                    washout_live_atr = (float(ab_volatile_anchor_px) - float(ab_volatile_extreme_px)) / max(float(atr_live), 1e-9)
                    rebound_live_atr = (float(q.close) - float(ab_volatile_extreme_px)) / max(float(atr_live), 1e-9)
                    two_ok = (prev_v3m_signed is not None and prev_v3m_signed > 0 and v3m_signed > 0)
                    if (
                        washout_live_atr >= float(ab_volatile_reversal_washout_live_atr_min)
                        and rebound_live_atr >= float(ab_volatile_reversal_rebound_live_atr_min)
                        and v3m_signed > deadzone
                        and vol_ratio >= float(ab_volatile_reversal_vol_min)
                        and ((not ab_volatile_reversal_require_two_positive) or two_ok)
                    ):
                        early_reversal_direction = "LONG"
                else:
                    washout_live_atr = (float(ab_volatile_extreme_px) - float(ab_volatile_anchor_px)) / max(float(atr_live), 1e-9)
                    rebound_live_atr = (float(ab_volatile_extreme_px) - float(q.close)) / max(float(atr_live), 1e-9)
                    two_ok = (prev_v3m_signed is not None and prev_v3m_signed < 0 and v3m_signed < 0)
                    if (
                        washout_live_atr >= float(ab_volatile_reversal_washout_live_atr_min)
                        and rebound_live_atr >= float(ab_volatile_reversal_rebound_live_atr_min)
                        and v3m_signed < -deadzone
                        and vol_ratio >= float(ab_volatile_reversal_vol_min)
                        and ((not ab_volatile_reversal_require_two_positive) or two_ok)
                    ):
                        early_reversal_direction = "SHORT"
            if ab_volatile_veto_direction is not None and direction == ab_volatile_veto_direction:
                if early_reversal_direction is not None:
                    direction = early_reversal_direction
                else:
                    blocks["AB_volatile_wrong_side_veto"] += 1
                    prev_vol_ratio = vol_ratio
                    prev_v3m_signed = v3m_signed
                    continue

        if short_exhaustion_enable and direction == "SHORT":
            win_start = ts - timedelta(seconds=max(60, int(short_exhaustion_window_sec)))
            win_prices = [q_prev.close for q_prev in q_hist if q_prev.ts >= win_start]
            bounce_ref = _close_at_or_before(ts - timedelta(seconds=max(5, int(short_exhaustion_bounce_sec))))
            if len(win_prices) >= 10 and bounce_ref is not None and atr_live and atr_live > 0:
                win_low = min(win_prices)
                win_high = max(win_prices)
                drop_live_atr = (win_high - float(q.close)) / max(float(atr_live), 1e-9)
                near_low_live_atr = (float(q.close) - win_low) / max(float(atr_live), 1e-9)
                bounce_live_atr = (float(q.close) - float(bounce_ref)) / max(float(atr_live), 1e-9)
                if (
                    drop_live_atr >= float(short_exhaustion_drop_live_atr)
                    and near_low_live_atr <= float(short_exhaustion_near_low_live_atr)
                    and bounce_live_atr >= float(short_exhaustion_bounce_live_atr)
                ):
                    blocks["SHORT_exhaustion_veto"] += 1
                    short_veto_until = ts + timedelta(seconds=max(10, int(short_exhaustion_cooldown_sec)))
                    prev_vol_ratio = vol_ratio
                    prev_v3m_signed = v3m_signed
                    continue

        if regime_exhaustion_enable:
            window_start = ts - timedelta(seconds=max(60, int(regime_window_sec)))
            win_prices = [q_prev.close for q_prev in q_hist if q_prev.ts >= window_start]
            stall_ref = _close_at_or_before(ts - timedelta(seconds=max(5, int(regime_stall_sec))))
            if len(win_prices) >= 10 and stall_ref is not None and atr_live and atr_live > 0:
                win_low = min(win_prices)
                win_high = max(win_prices)
                up_run = (float(q.close) - win_low) / max(float(atr_live), 1e-9)
                up_near_high = (win_high - float(q.close)) / max(float(atr_live), 1e-9)
                up_stall = (float(q.close) - float(stall_ref)) / max(float(atr_live), 1e-9)
                dn_run = (win_high - float(q.close)) / max(float(atr_live), 1e-9)
                dn_near_low = (float(q.close) - win_low) / max(float(atr_live), 1e-9)
                dn_stall = (float(stall_ref) - float(q.close)) / max(float(atr_live), 1e-9)
                if (
                    up_run >= float(regime_runup_live_atr)
                    and up_near_high <= float(regime_near_extreme_live_atr)
                    and up_stall <= float(regime_stall_live_atr)
                ):
                    regime_state = "WAIT_LONG_RESET"
                    regime_until = ts + timedelta(seconds=max(30, int(regime_cooldown_sec)))
                    regime_anchor_high = win_high
                    regime_anchor_low = None
                    blocks["REGIME_long_exhausted"] += 1
                if (
                    dn_run >= float(regime_runup_live_atr)
                    and dn_near_low <= float(regime_near_extreme_live_atr)
                    and dn_stall <= float(regime_stall_live_atr)
                ):
                    regime_state = "WAIT_SHORT_RESET"
                    regime_until = ts + timedelta(seconds=max(30, int(regime_cooldown_sec)))
                    regime_anchor_low = win_low
                    regime_anchor_high = None
                    blocks["REGIME_short_exhausted"] += 1

            if regime_state is not None:
                if regime_until is not None and ts > regime_until:
                    regime_state = None
                    regime_until = None
                    regime_anchor_high = None
                    regime_anchor_low = None
                else:
                    # During regime we only allow reversal entry path when pullback+rebound confirms.
                    reversal_entry: Optional[str] = None
                    if regime_state == "WAIT_LONG_RESET" and regime_anchor_high is not None:
                        pullback = (float(regime_anchor_high) - float(q.close)) / max(float(atr_live), 1e-9)
                        rb_ref = _close_at_or_before(ts - timedelta(seconds=max(5, int(regime_rebound_sec))))
                        rebound = ((float(q.close) - float(rb_ref)) / max(float(atr_live), 1e-9)) if rb_ref is not None else 0.0
                        two_pos_ok = (prev_v3m_signed is not None and prev_v3m_signed > 0 and v3m_signed > 0)
                        if pullback >= float(regime_pullback_live_atr) and rebound >= float(regime_rebound_live_atr) and vol_ratio >= float(regime_reversal_vol_min) and two_pos_ok:
                            reversal_entry = "LONG"
                    elif regime_state == "WAIT_SHORT_RESET" and regime_anchor_low is not None:
                        pullback = (float(q.close) - float(regime_anchor_low)) / max(float(atr_live), 1e-9)
                        rb_ref = _close_at_or_before(ts - timedelta(seconds=max(5, int(regime_rebound_sec))))
                        rebound = ((float(rb_ref) - float(q.close)) / max(float(atr_live), 1e-9)) if rb_ref is not None else 0.0
                        two_neg_ok = (prev_v3m_signed is not None and prev_v3m_signed < 0 and v3m_signed < 0)
                        if pullback >= float(regime_pullback_live_atr) and rebound >= float(regime_rebound_live_atr) and vol_ratio >= float(regime_reversal_vol_min) and two_neg_ok:
                            reversal_entry = "SHORT"
                    if reversal_entry is not None:
                        direction = reversal_entry
                        entry_path = "R"
                        regime_state = None
                        regime_until = None
                        regime_anchor_high = None
                        regime_anchor_low = None
                    else:
                        blocks["REGIME_wait_reset"] += 1
                        prev_vol_ratio = vol_ratio
                        prev_v3m_signed = v3m_signed
                        continue

        def _decay_ok(limit: float, lookback: int = 3) -> bool:
            if lookback < 2:
                lookback = 2
            need = lookback + 1
            if len(v3m_abs_hist) < need:
                return False
            prev_n = list(v3m_abs_hist)[-(lookback + 1) : -1]
            return v3_abs >= (max(prev_n) * float(limit))

        def _a_vol_threshold() -> float:
            t = ts.time()
            if t < time(11, 30):
                th = 1.35
            elif t < time(14, 0):
                th = 1.55
            else:
                th = 1.45
            if v3_abs >= v3m_fast:
                th = max(1.10, th - 0.10)
            return th

        if entry_path is None and b_time_start <= ts.time() <= b_time_end:
            b_sign_ok = (direction == "LONG" and v3m_signed > deadzone) or (direction == "SHORT" and v3m_signed < -deadzone)
            if not b_sign_ok:
                blocks["B_sign_mismatch"] += 1
            elif b_require_two_sign and (
                prev_v3m_signed is None
                or (direction == "LONG" and prev_v3m_signed <= 0)
                or (direction == "SHORT" and prev_v3m_signed >= 0)
            ):
                blocks["B_two_sign_not_confirmed"] += 1
            elif v3_abs < v3m_min:
                blocks["B_velocity_too_low"] += 1
            elif not _decay_ok(b_decay_limit, b_decay_lookback_sec):
                blocks["B_momentum_decay"] += 1
            elif b_require_prev_vol and (prev_vol_ratio is None or prev_vol_ratio < float(b_prev_vol_min)):
                blocks["B_prev_volume_too_low"] += 1
            else:
                t_vol_b = float(b_vol_fast) if v3_abs >= v3m_fast else float(b_vol_normal)
                if vol_ratio < t_vol_b:
                    blocks["B_volume_too_low"] += 1
                else:
                    ext_b = float(b_ext_base_mult) * atr_eff
                    if (
                        b_enable_burst_ext
                        and prev_vol_ratio is not None
                        and vol_ratio > 1.8
                        and prev_vol_ratio > 1.8
                    ):
                        ext_b = 2.50 * atr_eff
                    if dev_abs <= ext_b:
                        entry_path = "B"
                    else:
                        blocks["B_overextended"] += 1

        if entry_path is None and ts.time() <= a_latest_entry_time:
            if a_state == "WAIT_PULLBACK":
                if a_mark_ts is None or a_mark_direction is None:
                    a_state = "WAIT_MARK"
                else:
                    age_mark = (ts - a_mark_ts).total_seconds()
                    if age_mark > a_reentry_window_sec or a_mark_direction != direction:
                        a_state = "WAIT_MARK"
                        a_mark_ts = None
                        a_mark_direction = None
                    else:
                        pull_mult_now = a_pullback_mult_open if ts.time() < a_pullback_switch_time else a_pullback_mult_late
                        if dev_atr <= float(pull_mult_now):
                            a_state = "ARMED"
                            a_armed_ts = ts
                        else:
                            blocks["A_wait_pullback"] += 1
            elif a_state == "WAIT_MARK":
                if v3_abs >= v3m_min and dev_atr > float(a_overext_mult):
                    a_state = "WAIT_PULLBACK"
                    a_mark_ts = ts
                    a_mark_direction = direction
                    a_armed_ts = None
                    blocks["A_mark_overextended"] += 1
                else:
                    blocks["A_wait_mark"] += 1

            if a_state == "ARMED":
                if a_armed_ts is not None and (ts - a_armed_ts).total_seconds() > float(a_armed_ttl_sec):
                    blocks["A_armed_expired"] += 1
                    a_state = "WAIT_MARK"
                    a_mark_ts = None
                    a_mark_direction = None
                    a_armed_ts = None
                    prev_vol_ratio = vol_ratio
                    continue
                if dev_live_atr > float(a_entry_max_dev_atr):
                    blocks["A_entry_overextended"] += 1
                    a_state = "WAIT_PULLBACK"
                    a_mark_ts = ts
                    a_mark_direction = direction
                    a_armed_ts = None
                    prev_vol_ratio = vol_ratio
                    continue
                run_ref = None
                run_target = ts - timedelta(seconds=max(10, int(a_recent_runup_sec)))
                for q_prev in reversed(q_hist):
                    if q_prev.ts <= run_target:
                        run_ref = q_prev.close
                        break
                if run_ref is not None and atr_live and atr_live > 0:
                    run_signed = float(q.close - run_ref)
                    run_abs_live_atr = abs(run_signed) / max(float(atr_live), 1e-9)
                    if direction == "LONG" and run_signed > 0 and run_abs_live_atr > float(a_recent_runup_max_live_atr):
                        blocks["A_runup_overextended"] += 1
                        a_state = "WAIT_PULLBACK"
                        a_mark_ts = ts
                        a_mark_direction = direction
                        a_armed_ts = None
                        prev_vol_ratio = vol_ratio
                        continue
                    if direction == "SHORT" and run_signed < 0 and run_abs_live_atr > float(a_recent_runup_max_live_atr):
                        blocks["A_runup_overextended"] += 1
                        a_state = "WAIT_PULLBACK"
                        a_mark_ts = ts
                        a_mark_direction = direction
                        a_armed_ts = None
                        prev_vol_ratio = vol_ratio
                        continue
                if a_runup_guard_sec >= 30 and a_runup_guard_live_atr > 0:
                    guard_start = ts - timedelta(seconds=int(a_runup_guard_sec))
                    win_prices = [q_prev.close for q_prev in q_hist if q_prev.ts >= guard_start]
                    if len(win_prices) >= 5 and atr_live and atr_live > 0:
                        win_min = min(win_prices)
                        win_max = max(win_prices)
                        if direction == "LONG":
                            runup_live_atr = (q.close - win_min) / max(float(atr_live), 1e-9)
                            pullback_live_atr = (win_max - q.close) / max(float(atr_live), 1e-9)
                            if runup_live_atr > float(a_runup_guard_live_atr) and pullback_live_atr < float(a_runup_pullback_live_atr):
                                blocks["A_runup_guard_wait_pullback"] += 1
                                prev_vol_ratio = vol_ratio
                                continue
                        if direction == "SHORT":
                            runup_live_atr = (win_max - q.close) / max(float(atr_live), 1e-9)
                            pullback_live_atr = (q.close - win_min) / max(float(atr_live), 1e-9)
                            if runup_live_atr > float(a_runup_guard_live_atr) and pullback_live_atr < float(a_runup_pullback_live_atr):
                                blocks["A_runup_guard_wait_pullback"] += 1
                                prev_vol_ratio = vol_ratio
                                continue
                a_sign_ok = (direction == "LONG" and v3m_signed > deadzone) or (direction == "SHORT" and v3m_signed < -deadzone)
                if not a_sign_ok:
                    blocks["A_sign_mismatch"] += 1
                elif v3_abs < v3m_min:
                    blocks["A_velocity_too_low"] += 1
                elif not _decay_ok(float(a_decay_limit)):
                    blocks["A_momentum_decay"] += 1
                else:
                    t_vol_a = float(a_vol_min) if a_vol_min > 0 else _a_vol_threshold()
                    if vol_ratio < t_vol_a:
                        blocks["A_volume_too_low"] += 1
                    else:
                        entry_path = "A"

        if entry_path is not None and anti_decay_limit > 0 and anti_decay_lookback_sec >= 2:
            if not _decay_ok(float(anti_decay_limit), int(anti_decay_lookback_sec)):
                blocks["anti_momentum_decay"] += 1
                entry_path = None
        if entry_path is not None and direction == "LONG" and entry_long_runup_sec >= 30 and entry_long_runup_max_live_atr < 999:
            run_ref = _close_at_or_before(ts - timedelta(seconds=int(entry_long_runup_sec)))
            if run_ref is not None and atr_live and atr_live > 0:
                runup_live_atr = (float(q.close) - float(run_ref)) / max(float(atr_live), 1e-9)
                if runup_live_atr > float(entry_long_runup_max_live_atr):
                    blocks["entry_long_runup_veto"] += 1
                    entry_path = None
        if entry_path is not None and a_runup_guard_sec >= 30 and a_runup_guard_live_atr > 0:
            guard_start = ts - timedelta(seconds=int(a_runup_guard_sec))
            win_prices = [q_prev.close for q_prev in q_hist if q_prev.ts >= guard_start]
            if len(win_prices) >= 5 and atr_live and atr_live > 0:
                win_min = min(win_prices)
                win_max = max(win_prices)
                if direction == "LONG":
                    runup_live_atr = (q.close - win_min) / max(float(atr_live), 1e-9)
                    pullback_live_atr = (win_max - q.close) / max(float(atr_live), 1e-9)
                    if runup_live_atr > float(a_runup_guard_live_atr) and pullback_live_atr < float(a_runup_pullback_live_atr):
                        blocks["entry_runup_guard_wait_pullback"] += 1
                        entry_path = None
                if direction == "SHORT":
                    runup_live_atr = (win_max - q.close) / max(float(atr_live), 1e-9)
                    pullback_live_atr = (q.close - win_min) / max(float(atr_live), 1e-9)
                    if runup_live_atr > float(a_runup_guard_live_atr) and pullback_live_atr < float(a_runup_pullback_live_atr):
                        blocks["entry_runup_guard_wait_pullback"] += 1
                        entry_path = None

        prev_vol_ratio = vol_ratio
        prev_v3m_signed = v3m_signed

        if entry_path is None:
            continue
        if age_qqq_sec > qqq_trade_allow_age_sec:
            blocks["data_calc_only_qqq_stale"] += 1
            continue

        return DayResult(
            day=day,
            data_source=data_source,
            traded=True,
            mode=entry_path,
            direction=direction,
            entry_ts=ts.isoformat(),
            entry_px=float(round(q.close, 4)),
            entry_dev_atr=float(round(dev_live_atr, 4)),
            blocks_top=blocks.most_common(10),
        )

    return DayResult(
        day=day,
        data_source=data_source,
        traded=False,
        mode="",
        direction="",
        entry_ts="",
        entry_px=0.0,
        entry_dev_atr=0.0,
        blocks_top=blocks.most_common(10),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", action="append", required=True)
    ap.add_argument("--sfs-n", type=int, default=120000)
    ap.add_argument("--atr-seed-path", default="runtime/state/qqq_intraday_atr_seed.json")
    ap.add_argument("--ab-time-start", default="09:32:00")
    ap.add_argument("--ab-time-end", default="10:30:00")
    ap.add_argument("--b-time-start", default="09:32:00")
    ap.add_argument("--b-time-end", default="09:50:00")
    ap.add_argument("--qqq-max-age-sec", type=float, default=15.0)
    ap.add_argument("--qqq-trade-allow-age-sec", type=float, default=10.0)
    ap.add_argument("--a-overext-mult", type=float, default=1.60)
    ap.add_argument("--a-reentry-window-sec", type=float, default=90.0)
    ap.add_argument("--a-pullback-mult-open", type=float, default=1.50)
    ap.add_argument("--a-pullback-mult-late", type=float, default=1.40)
    ap.add_argument("--a-pullback-switch-time", default="10:00:00")
    ap.add_argument("--a-latest-entry-time", default="15:30:00")
    ap.add_argument("--a-entry-max-dev-atr", type=float, default=999.0)
    ap.add_argument("--a-armed-ttl-sec", type=float, default=99999.0)
    ap.add_argument("--a-recent-runup-sec", type=int, default=90)
    ap.add_argument("--a-recent-runup-max-live-atr", type=float, default=999.0)
    ap.add_argument("--a-runup-guard-sec", type=int, default=0)
    ap.add_argument("--a-runup-guard-live-atr", type=float, default=1.5)
    ap.add_argument("--a-runup-pullback-live-atr", type=float, default=0.35)
    ap.add_argument("--b-decay-limit", type=float, default=0.95)
    ap.add_argument("--a-decay-limit", type=float, default=0.95)
    ap.add_argument("--b-decay-lookback-sec", type=int, default=3)
    ap.add_argument("--b-vol-normal", type=float, default=1.35)
    ap.add_argument("--b-vol-fast", type=float, default=1.25)
    ap.add_argument("--b-require-two-sign", action="store_true")
    ap.add_argument("--b-require-prev-vol", action="store_true")
    ap.add_argument("--b-prev-vol-min", type=float, default=1.2)
    ap.add_argument("--b-ext-base-mult", type=float, default=1.6)
    ap.add_argument("--b-disable-burst-ext", action="store_true")
    ap.add_argument("--a-vol-min", type=float, default=0.0)
    ap.add_argument("--anti-decay-limit", type=float, default=0.0)
    ap.add_argument("--anti-decay-lookback-sec", type=int, default=0)
    ap.add_argument("--short-exhaustion-enable", action="store_true")
    ap.add_argument("--short-exhaustion-window-sec", type=int, default=480)
    ap.add_argument("--short-exhaustion-drop-live-atr", type=float, default=1.8)
    ap.add_argument("--short-exhaustion-near-low-live-atr", type=float, default=0.35)
    ap.add_argument("--short-exhaustion-bounce-sec", type=int, default=20)
    ap.add_argument("--short-exhaustion-bounce-live-atr", type=float, default=0.2)
    ap.add_argument("--short-exhaustion-cooldown-sec", type=int, default=180)
    ap.add_argument("--reversal-enable", action="store_true")
    ap.add_argument("--reversal-slope-sec", type=int, default=30)
    ap.add_argument("--reversal-slope-live-atr", type=float, default=0.15)
    ap.add_argument("--reversal-vol-min", type=float, default=1.2)
    ap.add_argument("--reversal-no-two-positive", action="store_true")
    ap.add_argument("--entry-long-runup-sec", type=int, default=0)
    ap.add_argument("--entry-long-runup-max-live-atr", type=float, default=999.0)
    ap.add_argument("--regime-exhaustion-enable", action="store_true")
    ap.add_argument("--regime-window-sec", type=int, default=480)
    ap.add_argument("--regime-runup-live-atr", type=float, default=2.0)
    ap.add_argument("--regime-near-extreme-live-atr", type=float, default=0.25)
    ap.add_argument("--regime-stall-sec", type=int, default=20)
    ap.add_argument("--regime-stall-live-atr", type=float, default=0.05)
    ap.add_argument("--regime-cooldown-sec", type=int, default=900)
    ap.add_argument("--regime-pullback-live-atr", type=float, default=1.0)
    ap.add_argument("--regime-rebound-sec", type=int, default=30)
    ap.add_argument("--regime-rebound-live-atr", type=float, default=0.25)
    ap.add_argument("--regime-reversal-vol-min", type=float, default=1.2)
    ap.add_argument("--ab-volatile-day-skip-enable", action="store_true")
    ap.add_argument("--ab-volatile-check-end", default="10:05:00")
    ap.add_argument("--ab-volatile-range-live-atr-min", type=float, default=4.5)
    ap.add_argument("--ab-volatile-directionality-live-atr-min", type=float, default=1.0)
    ap.add_argument("--ab-volatile-reversal-washout-live-atr-min", type=float, default=1.5)
    ap.add_argument("--ab-volatile-reversal-rebound-live-atr-min", type=float, default=0.8)
    ap.add_argument("--ab-volatile-no-early-reversal", action="store_true")
    ap.add_argument("--ab-volatile-reversal-vol-min", type=float, default=1.2)
    ap.add_argument("--ab-volatile-reversal-no-two-positive", action="store_true")
    ap.add_argument("--ab-volatile-no-probe", action="store_true")
    ap.add_argument("--ab-volatile-probe-break-live-atr", type=float, default=0.6)
    ap.add_argument("--ab-volatile-probe-reclaim-live-atr", type=float, default=0.2)
    ap.add_argument("--ab-volatile-keep-on-coarse-data", action="store_true")
    ap.add_argument("--out", default="runtime/state/qqq_ab_real_ticks.csv")
    args = ap.parse_args()

    rows: list[dict[str, Any]] = []
    atr_seed_path = Path(str(args.atr_seed_path))
    for day in args.day:
        ticks, data_source = asyncio.run(_load_ticks(day, int(args.sfs_n)))
        atr_seed_tqqq = _load_atr_seed(atr_seed_path, day)
        result = run_day(
            day=day,
            ticks=ticks,
            data_source=data_source,
            atr_seed_tqqq=atr_seed_tqqq,
            ab_time_start=time.fromisoformat(str(args.ab_time_start)),
            ab_time_end=time.fromisoformat(str(args.ab_time_end)),
            b_time_start=time.fromisoformat(str(args.b_time_start)),
            b_time_end=time.fromisoformat(str(args.b_time_end)),
            qqq_max_age_sec=float(args.qqq_max_age_sec),
            qqq_trade_allow_age_sec=float(args.qqq_trade_allow_age_sec),
            a_overext_mult=float(args.a_overext_mult),
            a_reentry_window_sec=float(args.a_reentry_window_sec),
            a_pullback_mult_open=float(args.a_pullback_mult_open),
            a_pullback_mult_late=float(args.a_pullback_mult_late),
            a_pullback_switch_time=time.fromisoformat(str(args.a_pullback_switch_time)),
            a_latest_entry_time=time.fromisoformat(str(args.a_latest_entry_time)),
            a_entry_max_dev_atr=float(args.a_entry_max_dev_atr),
            a_armed_ttl_sec=float(args.a_armed_ttl_sec),
            a_recent_runup_sec=int(args.a_recent_runup_sec),
            a_recent_runup_max_live_atr=float(args.a_recent_runup_max_live_atr),
            a_runup_guard_sec=int(args.a_runup_guard_sec),
            a_runup_guard_live_atr=float(args.a_runup_guard_live_atr),
            a_runup_pullback_live_atr=float(args.a_runup_pullback_live_atr),
            b_decay_limit=float(args.b_decay_limit),
            a_decay_limit=float(args.a_decay_limit),
            b_decay_lookback_sec=int(args.b_decay_lookback_sec),
            b_vol_normal=float(args.b_vol_normal),
            b_vol_fast=float(args.b_vol_fast),
            b_require_two_sign=bool(args.b_require_two_sign),
            b_require_prev_vol=bool(args.b_require_prev_vol),
            b_prev_vol_min=float(args.b_prev_vol_min),
            b_ext_base_mult=float(args.b_ext_base_mult),
            b_enable_burst_ext=not bool(args.b_disable_burst_ext),
            a_vol_min=float(args.a_vol_min),
            anti_decay_limit=float(args.anti_decay_limit),
            anti_decay_lookback_sec=int(args.anti_decay_lookback_sec),
            short_exhaustion_enable=bool(args.short_exhaustion_enable),
            short_exhaustion_window_sec=int(args.short_exhaustion_window_sec),
            short_exhaustion_drop_live_atr=float(args.short_exhaustion_drop_live_atr),
            short_exhaustion_near_low_live_atr=float(args.short_exhaustion_near_low_live_atr),
            short_exhaustion_bounce_sec=int(args.short_exhaustion_bounce_sec),
            short_exhaustion_bounce_live_atr=float(args.short_exhaustion_bounce_live_atr),
            short_exhaustion_cooldown_sec=int(args.short_exhaustion_cooldown_sec),
            reversal_enable=bool(args.reversal_enable),
            reversal_slope_sec=int(args.reversal_slope_sec),
            reversal_slope_live_atr=float(args.reversal_slope_live_atr),
            reversal_vol_min=float(args.reversal_vol_min),
            reversal_require_two_positive=not bool(args.reversal_no_two_positive),
            entry_long_runup_sec=int(args.entry_long_runup_sec),
            entry_long_runup_max_live_atr=float(args.entry_long_runup_max_live_atr),
            regime_exhaustion_enable=bool(args.regime_exhaustion_enable),
            regime_window_sec=int(args.regime_window_sec),
            regime_runup_live_atr=float(args.regime_runup_live_atr),
            regime_near_extreme_live_atr=float(args.regime_near_extreme_live_atr),
            regime_stall_sec=int(args.regime_stall_sec),
            regime_stall_live_atr=float(args.regime_stall_live_atr),
            regime_cooldown_sec=int(args.regime_cooldown_sec),
            regime_pullback_live_atr=float(args.regime_pullback_live_atr),
            regime_rebound_sec=int(args.regime_rebound_sec),
            regime_rebound_live_atr=float(args.regime_rebound_live_atr),
            regime_reversal_vol_min=float(args.regime_reversal_vol_min),
            ab_volatile_day_skip_enable=bool(args.ab_volatile_day_skip_enable),
            ab_volatile_check_end=time.fromisoformat(str(args.ab_volatile_check_end)),
            ab_volatile_range_live_atr_min=float(args.ab_volatile_range_live_atr_min),
            ab_volatile_directionality_live_atr_min=float(args.ab_volatile_directionality_live_atr_min),
            ab_volatile_reversal_washout_live_atr_min=float(args.ab_volatile_reversal_washout_live_atr_min),
            ab_volatile_reversal_rebound_live_atr_min=float(args.ab_volatile_reversal_rebound_live_atr_min),
            ab_volatile_allow_early_reversal=not bool(args.ab_volatile_no_early_reversal),
            ab_volatile_reversal_vol_min=float(args.ab_volatile_reversal_vol_min),
            ab_volatile_reversal_require_two_positive=not bool(args.ab_volatile_reversal_no_two_positive),
            ab_volatile_probe_enable=not bool(args.ab_volatile_no_probe),
            ab_volatile_probe_break_live_atr=float(args.ab_volatile_probe_break_live_atr),
            ab_volatile_probe_reclaim_live_atr=float(args.ab_volatile_probe_reclaim_live_atr),
            ab_volatile_disable_on_coarse_data=not bool(args.ab_volatile_keep_on_coarse_data),
        )
        rows.append(
            {
                "day": result.day,
                "data_source": result.data_source,
                "traded": int(result.traded),
                "mode": result.mode,
                "direction": result.direction,
                "entry_ts": result.entry_ts,
                "entry_px": result.entry_px,
                "entry_dev_atr": result.entry_dev_atr,
                "top_blocked": json.dumps(result.blocks_top, ensure_ascii=False),
            }
        )

    out = Path(str(args.out))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(
            f,
            fieldnames=["day", "data_source", "traded", "mode", "direction", "entry_ts", "entry_px", "entry_dev_atr", "top_blocked"],
        )
        wr.writeheader()
        wr.writerows(rows)
    print(out)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
