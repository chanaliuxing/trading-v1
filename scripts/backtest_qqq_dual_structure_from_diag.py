from __future__ import annotations

import argparse
import json
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

ET = datetime.now().astimezone().tzinfo


@dataclass
class Tick:
    ts: datetime
    px: float
    vwap: float
    atr_cap: float
    v3m_signed: float
    vol_ratio: float
    data_age_qqq_sec: float


@dataclass
class DayResult:
    day: str
    traded: bool
    mode: str
    direction: str
    entry_ts: str
    entry_px: float
    entry_dev_atr: float
    pullback_mult: float
    blocks_top: list[tuple[str, int]]


def _load_ticks(diag_path: Path, day: str, end_hhmm: str, *, ffill_missing: bool = False) -> list[Tick]:
    end_t = time.fromisoformat(end_hhmm)
    out: list[Tick] = []
    last_px = 0.0
    last_vwap = 0.0
    last_atr = 0.0
    last_v3 = 0.0
    last_vol = 0.0
    with diag_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") != "entry_gate":
                continue
            ts_raw = row.get("ts_now_et")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except Exception:
                continue
            if ts.date().isoformat() != day:
                continue
            if ts.time() < time(9, 32) or ts.time() > end_t:
                continue
            px = float(row.get("qqq_close_now") or 0.0)
            vwap = float(row.get("vwap_now") or 0.0)
            atr = float(row.get("atr_cap") or 0.0)
            v3 = float(row.get("v3m_signed") or 0.0)
            vol = float(row.get("vol_surge_ratio") or 0.0)
            if ffill_missing:
                if px <= 0 and last_px > 0:
                    px = last_px
                if vwap <= 0 and last_vwap > 0:
                    vwap = last_vwap
                if atr <= 0 and last_atr > 0:
                    atr = last_atr
                if v3 == 0.0 and last_v3 != 0.0:
                    v3 = last_v3
                if vol <= 0 and last_vol > 0:
                    vol = last_vol
            if px > 0:
                last_px = px
            if vwap > 0:
                last_vwap = vwap
            if atr > 0:
                last_atr = atr
            if v3 != 0.0:
                last_v3 = v3
            if vol > 0:
                last_vol = vol
            age = float(row.get("data_age_qqq_sec") or 9999.0)
            out.append(Tick(ts=ts, px=px, vwap=vwap, atr_cap=atr, v3m_signed=v3, vol_ratio=vol, data_age_qqq_sec=age))
    out.sort(key=lambda x: x.ts)
    return out


def _pass_decay(v3_hist: deque[float], now_abs: float, decay_limit: float = 0.95) -> bool:
    if len(v3_hist) < 3:
        return False
    prev_max = max(v3_hist[-1], v3_hist[-2], v3_hist[-3])
    return now_abs >= (float(decay_limit) * prev_max)


def _t_vol_a(ts: datetime, v3_abs: float, v3_fast: float) -> float:
    t = ts.time()
    if t < time(11, 30):
        th = 1.35
    elif t < time(14, 0):
        th = 1.55
    else:
        th = 1.45
    if v3_abs >= v3_fast:
        th = max(1.10, th - 0.10)
    return th


def run_day(
    ticks: list[Tick],
    day: str,
    *,
    gate0_mode: str = "strict",
    a_pullback_mult: float = 1.10,
    dynamic_pullback: bool = False,
    b_decay_limit: float = 0.95,
    qqq_max_age_sec: float = 15.0,
    b_time_start: time = time(9, 32),
    b_time_end: time = time(10, 15),
    a_pullback_mult_open: float = 1.50,
    a_pullback_mult_late: float = 1.40,
    a_pullback_switch_time: time = time(10, 30),
    b_vol_normal: float = 1.35,
    b_vol_fast: float = 1.25,
) -> DayResult:
    if not ticks:
        return DayResult(day=day, traded=False, mode="", direction="", entry_ts="", entry_px=0.0, entry_dev_atr=0.0, pullback_mult=float(a_pullback_mult), blocks_top=[("no_data", 1)])

    trade_count = 0
    blocks = Counter()
    qqq_ts = deque()
    v3_abs_hist: deque[float] = deque(maxlen=10)
    vol_prev: Optional[float] = None
    a_state = "IDLE"
    a_mark_ts: Optional[datetime] = None
    a_mark_dir: Optional[str] = None
    a_armed_dir: Optional[str] = None

    pullback_mult = float(a_pullback_mult)
    if dynamic_pullback:
        open_ticks = [x for x in ticks if time(9, 32) <= x.ts.time() <= time(9, 50) and x.px > 0 and x.atr_cap > 0]
        if open_ticks:
            px_vals = [x.px for x in open_ticks]
            atr_vals = sorted([x.atr_cap for x in open_ticks if x.atr_cap > 0])
            med_atr = atr_vals[len(atr_vals) // 2] if atr_vals else 0.0
            if med_atr > 0:
                open_amp_atr = (max(px_vals) - min(px_vals)) / med_atr
                pullback_mult = 1.5 if open_amp_atr >= 4.0 else 1.4

    for x in ticks:
        if trade_count >= 1:
            break

        if not (time(9, 32) <= x.ts.time() <= time(15, 30)):
            blocks["time_window"] += 1
            continue

        if x.px <= 0 or x.vwap <= 0 or x.atr_cap <= 0:
            blocks["data_not_ready_qqq"] += 1
            continue
        if x.data_age_qqq_sec > float(qqq_max_age_sec):
            blocks["data_not_ready_qqq"] += 1
            continue
        if gate0_mode == "strict":
            if x.data_age_qqq_sec > 3.0:
                blocks["data_not_ready_qqq"] += 1
                continue
            qqq_ts.append(x.ts)
            cutoff = x.ts - timedelta(seconds=180)
            while qqq_ts and qqq_ts[0] < cutoff:
                qqq_ts.popleft()
            if len(qqq_ts) < 120:
                blocks["data_not_ready_qqq"] += 1
                continue
            if (qqq_ts[-1] - qqq_ts[0]).total_seconds() < 175:
                blocks["data_not_ready_qqq"] += 1
                continue
            gaps_ok = True
            prev_ts = qqq_ts[0]
            for cur in list(qqq_ts)[1:]:
                if (cur - prev_ts).total_seconds() > 5:
                    gaps_ok = False
                    break
                prev_ts = cur
            if not gaps_ok:
                blocks["data_not_ready_qqq"] += 1
                continue

        if x.px > x.vwap:
            direction = "LONG"
        elif x.px < x.vwap:
            direction = "SHORT"
        else:
            blocks["dir_none"] += 1
            continue

        dev = abs(x.px - x.vwap)
        deadzone = 0.15 * x.atr_cap
        v3_abs = abs(x.v3m_signed)
        v3_min = 0.35 * x.atr_cap
        v3_fast = 0.50 * x.atr_cap

        pullback_mult_now = float(a_pullback_mult_open if x.ts.time() < a_pullback_switch_time else a_pullback_mult_late)

        def _eval_b() -> bool:
            if direction == "LONG" and not (x.v3m_signed > deadzone):
                blocks["B_sign_mismatch"] += 1
                return False
            if direction == "SHORT" and not (x.v3m_signed < -deadzone):
                blocks["B_sign_mismatch"] += 1
                return False
            if v3_abs < v3_min:
                blocks["B_velocity_too_low"] += 1
                return False
            if not _pass_decay(v3_abs_hist, v3_abs, decay_limit=b_decay_limit):
                blocks["B_momentum_decay"] += 1
                return False
            t_vol_b = float(b_vol_fast) if v3_abs >= v3_fast else float(b_vol_normal)
            if x.vol_ratio < t_vol_b:
                blocks["B_volume_too_low"] += 1
                return False
            ext_b = 1.6 * x.atr_cap
            if vol_prev is not None and x.vol_ratio > 1.8 and vol_prev > 1.8:
                ext_b = 2.5 * x.atr_cap
            if dev > ext_b:
                blocks["B_overextended"] += 1
                return False
            return True

        def _eval_a() -> bool:
            nonlocal a_state, a_mark_ts, a_mark_dir, a_armed_dir
            if a_state == "MARKED" and a_mark_ts is not None and (x.ts - a_mark_ts).total_seconds() > 90:
                a_state = "IDLE"
                a_mark_ts = None
                a_mark_dir = None
                a_armed_dir = None

            if a_state == "IDLE":
                if v3_abs >= v3_min and dev > (1.6 * x.atr_cap):
                    a_state = "MARKED"
                    a_mark_ts = x.ts
                    a_mark_dir = direction
                    blocks["A_mark_overextended"] += 1
                else:
                    blocks["A_wait_mark"] += 1
                return False

            if a_state == "MARKED":
                if a_mark_dir != direction:
                    a_state = "IDLE"
                    a_mark_ts = None
                    a_mark_dir = None
                    blocks["A_dir_changed"] += 1
                    return False
                if dev <= (float(pullback_mult_now) * x.atr_cap):
                    a_state = "ARMED"
                    a_armed_dir = direction
                else:
                    blocks["A_wait_pullback"] += 1
                    return False

            if a_state != "ARMED" or a_armed_dir != direction:
                blocks["A_not_armed"] += 1
                return False

            if direction == "LONG" and not (x.v3m_signed > deadzone):
                blocks["A_sign_mismatch"] += 1
                return False
            if direction == "SHORT" and not (x.v3m_signed < -deadzone):
                blocks["A_sign_mismatch"] += 1
                return False
            if not _pass_decay(v3_abs_hist, v3_abs, decay_limit=0.95):
                blocks["A_momentum_decay"] += 1
                return False
            t_vol = _t_vol_a(x.ts, v3_abs, v3_fast)
            if x.vol_ratio < t_vol:
                blocks["A_volume_too_low"] += 1
                return False
            return True

        allowed = False
        mode = ""
        if a_state == "ARMED":
            if _eval_a():
                allowed = True
                mode = "A"
        elif b_time_start <= x.ts.time() <= b_time_end:
            if _eval_b():
                allowed = True
                mode = "B"
            else:
                _eval_a()
        else:
            _eval_a()

        v3_abs_hist.append(v3_abs)
        vol_prev = x.vol_ratio

        if allowed:
            trade_count = 1
            return DayResult(
                day=day,
                traded=True,
                mode=mode,
                direction=direction,
                entry_ts=x.ts.isoformat(),
                entry_px=float(round(x.px, 4)),
                entry_dev_atr=float(round(dev / max(x.atr_cap, 1e-9), 4)),
                pullback_mult=float(pullback_mult_now),
                blocks_top=blocks.most_common(8),
            )

    return DayResult(day=day, traded=False, mode="", direction="", entry_ts="", entry_px=0.0, entry_dev_atr=0.0, pullback_mult=float(pullback_mult), blocks_top=blocks.most_common(8))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", default="runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl")
    ap.add_argument("--day", action="append", required=True)
    ap.add_argument("--morning-end", default="12:00")
    ap.add_argument("--gate0-mode", choices=["strict", "minimal"], default="strict")
    ap.add_argument("--a-pullback-mult", type=float, default=1.10)
    ap.add_argument("--dynamic-pullback", action="store_true")
    ap.add_argument("--b-decay-limit", type=float, default=0.95)
    ap.add_argument("--qqq-max-age-sec", type=float, default=15.0)
    ap.add_argument("--b-time-start", default="09:32:00")
    ap.add_argument("--b-time-end", default="10:15:00")
    ap.add_argument("--a-pullback-mult-open", type=float, default=1.50)
    ap.add_argument("--a-pullback-mult-late", type=float, default=1.40)
    ap.add_argument("--a-pullback-switch-time", default="10:30:00")
    ap.add_argument("--b-vol-normal", type=float, default=1.35)
    ap.add_argument("--b-vol-fast", type=float, default=1.25)
    ap.add_argument("--out", default="runtime/state/qqq_dual_structure_replay.csv")
    args = ap.parse_args()

    diag_path = Path(args.diag)
    rows: list[dict[str, Any]] = []
    for day in args.day:
        ticks = _load_ticks(diag_path, day, args.morning_end, ffill_missing=(str(args.gate0_mode) == "minimal"))
        r = run_day(
            ticks,
            day,
            gate0_mode=str(args.gate0_mode),
            a_pullback_mult=float(args.a_pullback_mult),
            dynamic_pullback=bool(args.dynamic_pullback),
            b_decay_limit=float(args.b_decay_limit),
            qqq_max_age_sec=float(args.qqq_max_age_sec),
            b_time_start=time.fromisoformat(str(args.b_time_start)),
            b_time_end=time.fromisoformat(str(args.b_time_end)),
            a_pullback_mult_open=float(args.a_pullback_mult_open),
            a_pullback_mult_late=float(args.a_pullback_mult_late),
            a_pullback_switch_time=time.fromisoformat(str(args.a_pullback_switch_time)),
            b_vol_normal=float(args.b_vol_normal),
            b_vol_fast=float(args.b_vol_fast),
        )
        rows.append(
            {
                "day": r.day,
                "traded": int(r.traded),
                "mode": r.mode,
                "direction": r.direction,
                "entry_ts": r.entry_ts,
                "entry_px": r.entry_px,
                "entry_dev_atr": r.entry_dev_atr,
                "pullback_mult": r.pullback_mult,
                "top_blocked": json.dumps(r.blocks_top, ensure_ascii=False),
            }
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    import csv

    with out.open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["day", "traded", "mode", "direction", "entry_ts", "entry_px", "entry_dev_atr", "pullback_mult", "top_blocked"])
        wr.writeheader()
        wr.writerows(rows)
    print(out)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
