#!/usr/bin/env python3
"""
Generate one-file QQQ intraday diagnostic report from local artifacts.

Inputs:
- trader.log (JSON lines)
- runtime/state/qqq_intraday_diag_<strategy_id>.jsonl
- runtime/state/qqq_intraday_<strategy_id>.json
- runtime/state/qqq_intraday_observability_<strategy_id>.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


@dataclass
class Row:
    source: str
    ts: str
    event: str
    level: str
    strategy_id: str
    payload: dict[str, Any]


KEY_EVENTS = (
    "qqq_intraday_initialized",
    "qqq_state_restored",
    "qqq_intraday_daily_reset",
    "qqq_price_base_locked",
    "price_base_window_missed",
    "qqq_price_0945_locked",
    "qqq_amp_locked",
    "qqq_deadwater",
    "EMERGENCY_ATR_ACTIVE",
    "qqq_entry_blocked",
    "qqq_entered",
    "qqq_break_even_armed",
    "qqq_stop_widened",
    "qqq_flatten_submitted",
    "qqq_position_reconciled_live",
    "qqq_position_reconciled_flat",
    "qqq_intraday_fatal",
    "qqq_intraday_loop_error",
    "qqq_intraday_shutdown",
)

ORDER_OR_FILL_HINTS = (
    "order",
    "fill",
    "intent",
    "entered",
    "flatten",
    "reconciled",
    "submit",
)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generate consolidated QQQ daily report")
    now_day = datetime.now(ET).strftime("%Y-%m-%d")
    ap.add_argument("--day", default=now_day, help="ET day, format YYYY-MM-DD")
    ap.add_argument("--strategy-id", default="qqq_intraday_001")
    ap.add_argument("--from", dest="from_hm", default="09:30", help="ET start time HH:MM")
    ap.add_argument("--to", dest="to_hm", default="16:00", help="ET end time HH:MM")
    ap.add_argument(
        "--log-path",
        default=os.path.expanduser("~/grafana-stack/logs/trader.log"),
        help="Path to trader.log",
    )
    ap.add_argument("--state-dir", default="runtime/state")
    ap.add_argument("--out", default=None, help="Markdown output path")
    ap.add_argument("--stdout", action="store_true", help="Print report to stdout")
    return ap.parse_args()


def _json_lines(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _dt(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=ET)
    return d


def _minute_key(ts: str) -> str:
    return _dt(ts).replace(second=0, microsecond=0).isoformat()


def _is_target_day(ts: str, day: str) -> bool:
    return ts.startswith(day)


def _parse_hm(hm: str) -> tuple[int, int]:
    hh, mm = hm.split(":")
    return int(hh), int(mm)


def _in_time_range(ts: str, day: str, from_hm: str, to_hm: str) -> bool:
    if not _is_target_day(ts, day):
        return False
    dt = _dt(ts).astimezone(ET)
    fh, fm = _parse_hm(from_hm)
    th, tm = _parse_hm(to_hm)
    start = dt.replace(hour=fh, minute=fm, second=0, microsecond=0)
    end = dt.replace(hour=th, minute=tm, second=59, microsecond=999999)
    return start <= dt <= end


def _load_trader_rows(log_path: Path, day: str, strategy_id: str, from_hm: str, to_hm: str) -> list[Row]:
    out: list[Row] = []
    for obj in _json_lines(log_path):
        ts = str(obj.get("timestamp") or "")
        if not _in_time_range(ts, day, from_hm, to_hm):
            continue
        event = str(obj.get("event") or "")
        logger = str(obj.get("logger") or "")
        sid = str(obj.get("strategy_id") or "")
        related = (
            sid == strategy_id
            or "qqq_" in event
            or "strategies.qqq_intraday_strategy" in logger
            or event
            in {
                "Initiating graceful shutdown... (Press Ctrl+C again to force exit)",
                "✅ System shutdown complete",
            }
        )
        if not related:
            continue
        out.append(
            Row(
                source="trader.log",
                ts=ts,
                event=event,
                level=str(obj.get("level") or ""),
                strategy_id=sid,
                payload=obj,
            )
        )
    out.sort(key=lambda r: r.ts)
    return out


def _load_diag_rows(diag_path: Path, day: str, strategy_id: str, from_hm: str, to_hm: str) -> list[Row]:
    out: list[Row] = []
    for obj in _json_lines(diag_path):
        ts = str(obj.get("ts_now_et") or "")
        if not _in_time_range(ts, day, from_hm, to_hm):
            continue
        sid = str(obj.get("strategy_id") or "")
        if sid and sid != strategy_id:
            continue
        out.append(
            Row(
                source="diag",
                ts=ts,
                event=str(obj.get("event") or ""),
                level="info",
                strategy_id=sid,
                payload=obj,
            )
        )
    out.sort(key=lambda r: r.ts)
    return out


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _pick_first(rows: list[Row], event: str) -> Optional[Row]:
    for r in rows:
        if r.event == event:
            return r
    return None


def _pick_last(rows: list[Row], event: str) -> Optional[Row]:
    for r in reversed(rows):
        if r.event == event:
            return r
    return None


def _analyze_heartbeat(diag_rows: list[Row]) -> dict[str, Any]:
    hb = [r for r in diag_rows if r.event == "qqq_loop_heartbeat"]
    if len(hb) < 2:
        return {"count": len(hb), "max_gap_sec": None, "gap_over_90s": 0}
    gaps = []
    for i in range(1, len(hb)):
        t1 = _dt(hb[i - 1].ts)
        t2 = _dt(hb[i].ts)
        gaps.append((t2 - t1).total_seconds())
    return {
        "count": len(hb),
        "max_gap_sec": round(max(gaps), 3),
        "gap_over_90s": sum(1 for g in gaps if g > 90.0),
    }


def _fmt_num(x: Any, digits: int = 3) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
        return f"{v:.{digits}f}"
    except Exception:
        return str(x)


def _infer_order_fill_rows(trader_rows: list[Row]) -> list[Row]:
    out: list[Row] = []
    for r in trader_rows:
        e = r.event.lower()
        if any(h in e for h in ORDER_OR_FILL_HINTS):
            out.append(r)
            continue
        text = json.dumps(r.payload, ensure_ascii=False).lower()
        if any(h in text for h in ORDER_OR_FILL_HINTS):
            out.append(r)
    return out


def _build_runs(trader_rows: list[Row]) -> list[dict[str, Any]]:
    starts = [r for r in trader_rows if r.event == "qqq_intraday_initialized"]
    if not starts:
        return []

    rows = sorted(trader_rows, key=lambda r: r.ts)
    runs: list[dict[str, Any]] = []
    for i, s in enumerate(starts):
        end_ts = starts[i + 1].ts if i + 1 < len(starts) else None
        chunk = [r for r in rows if r.ts >= s.ts and (end_ts is None or r.ts < end_ts)]
        run = {
            "start": s.ts,
            "end": chunk[-1].ts if chunk else s.ts,
            "price_base_locked": _pick_first(chunk, "qqq_price_base_locked") is not None,
            "price_0945_locked": _pick_first(chunk, "qqq_price_0945_locked") is not None,
            "amp_locked": _pick_first(chunk, "qqq_amp_locked") is not None,
            "entered": _pick_first(chunk, "qqq_entered") is not None,
            "entry_blocked_reasons": Counter(
                str(r.payload.get("reason") or "") for r in chunk if r.event == "qqq_entry_blocked"
            ),
            "daily_reset": _pick_first(chunk, "qqq_intraday_daily_reset") is not None,
        }
        run["entry_blocked_reasons"].pop("", None)
        runs.append(run)
    return runs


def _build_minute_timeline(diag_rows: list[Row], trader_rows: list[Row]) -> list[dict[str, Any]]:
    timeline: dict[str, dict[str, Any]] = {}

    def _slot(ts: str) -> dict[str, Any]:
        m = _minute_key(ts)
        if m not in timeline:
            timeline[m] = {
                "minute": m,
                "hb": None,
                "gate": None,
                "missing": None,
                "orders": [],
                "fills": [],
                "events": [],
            }
        return timeline[m]

    for r in diag_rows:
        slot = _slot(r.ts)
        if r.event == "qqq_loop_heartbeat":
            slot["hb"] = r.payload
        elif r.event == "qqq_entry_blocked":
            slot["gate"] = r.payload
        elif r.event in {
            "qqq_entered",
            "qqq_flatten_submitted",
            "qqq_position_reconciled_live",
            "qqq_position_reconciled_flat",
            "qqq_amp_locked",
            "qqq_price_0945_locked",
            "qqq_price_base_locked",
            "qqq_deadwater",
        }:
            slot["events"].append(r.event)

    milestone_events = {
        "qqq_intraday_initialized",
        "qqq_price_base_locked",
        "qqq_price_0945_locked",
        "qqq_amp_locked",
        "qqq_deadwater",
        "qqq_entered",
        "qqq_flatten_submitted",
        "qqq_position_reconciled_live",
        "qqq_position_reconciled_flat",
    }

    for r in trader_rows:
        e = r.event.lower()
        is_missing = r.event == "qqq_required_data_missing(debug)"
        is_fill = "fill" in e
        is_orderish = any(k in e for k in ("order", "intent", "submit", "entered", "flatten", "reconciled"))
        is_milestone = r.event in milestone_events
        if not (is_missing or is_fill or is_orderish or is_milestone):
            continue

        slot = _slot(r.ts)
        if is_missing:
            slot["missing"] = r.payload.get("missing")
        if is_fill:
            slot["fills"].append(r.event)
        if is_orderish:
            slot["orders"].append(r.event)
        if is_milestone:
            slot["events"].append(r.event)

    out = [timeline[k] for k in sorted(timeline.keys())]
    out = [
        r
        for r in out
        if (
            r.get("hb")
            or r.get("gate")
            or r.get("missing")
            or r.get("orders")
            or r.get("fills")
            or r.get("events")
        )
    ]
    return out


def _gate_matrix(gate: dict[str, Any]) -> str:
    if not gate:
        return "data_live=-<br>qqq_trend=-<br>qqew_trend=-<br>add_dir=-<br>strength=-<br>atr=-<br>qty=-"
    reason = str(gate.get("reason") or "")
    long_ok = gate.get("long_ok") is True
    short_ok = gate.get("short_ok") is True
    qqq_trend_pass = long_ok or short_ok

    # New schema: explicit qqq/qqew fields; old schema only has long_ok/short_ok.
    qqq_vwap = gate.get("qqq_vwap", gate.get("vwap"))
    qqq_last = gate.get("qqq_last")
    qqew_vwap = gate.get("qqew_vwap")
    qqew_last = gate.get("qqew_last")
    qqq_pass = qqq_last is not None and qqq_vwap is not None and qqq_trend_pass
    qqew_pass = qqew_last is not None and qqew_vwap is not None and qqq_trend_pass

    add_dir_pass = False
    if gate.get("add_now") is not None and gate.get("add_ago") is not None:
        try:
            vel = float(gate.get("add_now")) - float(gate.get("add_ago"))
            dir_deadzone = float(gate.get("add_direction_deadzone", 10.0))
            add_dir_pass = ((not long_ok or vel > dir_deadzone) and (not short_ok or vel < -dir_deadzone))
        except Exception:
            add_dir_pass = False

    # New schema: strength_ok/add_level_threshold/add_velocity_threshold.
    # Old schema fallback: use add_threshold/deviation as legacy proxy.
    strength_pass = False
    if gate.get("strength_ok") is not None:
        strength_pass = bool(gate.get("strength_ok"))
    elif gate.get("add_now") is not None and gate.get("add_ago") is not None and gate.get("add_threshold") is not None:
        try:
            vel = float(gate.get("add_now")) - float(gate.get("add_ago"))
            th = float(gate.get("add_threshold"))
            strength_pass = abs(vel) >= th
        except Exception:
            strength_pass = False

    data_live_pass = reason != "data_stale"
    atr_pass = gate.get("atr") is not None and float(gate.get("atr") or 0.0) > 0.0
    qty_pass = reason != "qty_zero"
    if reason == "already_traded":
        qty_pass = True

    def pf(v: bool) -> str:
        return "PASS" if v else "FAIL"
    return "<br>".join(
        [
            f"data_live={pf(data_live_pass)}",
            f"qqq_trend={pf(qqq_pass)}",
            f"qqew_trend={pf(qqew_pass)}",
            f"add_dir={pf(add_dir_pass)}",
            f"strength={pf(strength_pass)}",
            f"atr={pf(atr_pass)}",
            f"qty={pf(qty_pass)}",
        ]
    )


def _render_minute_table(rows: list[dict[str, Any]], limit: int = 240) -> list[str]:
    lines: list[str] = []
    lines.append("| ET分钟 | 数据状态 | Gate细节 | Gate Matrix | 订单/成交 | 未交易原因 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    shown = rows[-limit:]
    for row in shown:
        minute = _dt(row["minute"]).strftime("%H:%M")
        hb = row.get("hb") or {}
        gate = row.get("gate") or {}
        missing = row.get("missing")

        age_q = hb.get("data_age_qqq_sec")
        age_qqew = hb.get("data_age_qqew_sec")
        age_add = hb.get("data_age_add_sec")
        data_parts = []
        if hb:
            data_parts.append(f"is_traded={hb.get('is_traded')}")
            data_parts.append(f"amp_locked={hb.get('amp_locked')}")
            data_parts.append(f"qqq_age={_fmt_num(age_q, 1)}s")
            if age_qqew is not None:
                data_parts.append(f"qqew_age={_fmt_num(age_qqew, 1)}s")
            data_parts.append(f"add_age={_fmt_num(age_add, 1)}s")
        if missing:
            data_parts.append(f"missing={missing}")
        data_status = "<br>".join(data_parts) if data_parts else "-"

        gate_reason = str(gate.get("reason") or "-")
        gate_parts = [gate_reason]
        if gate:
            qqq_vwap = gate.get("qqq_vwap", gate.get("vwap"))
            qqew_vwap = gate.get("qqew_vwap")
            velocity = gate.get("velocity")
            if velocity is None and gate.get("add_now") is not None and gate.get("add_ago") is not None:
                try:
                    velocity = float(gate.get("add_now")) - float(gate.get("add_ago"))
                except Exception:
                    velocity = None
            level = gate.get("level", gate.get("add_now"))
            strength = gate.get("strength_ok")
            strength_metric = None
            try:
                if level is not None and velocity is not None:
                    strength_metric = max(abs(float(level)), abs(float(velocity)))
            except Exception:
                strength_metric = None
            gate_parts.append(f"long_ok={gate.get('long_ok')}")
            gate_parts.append(f"short_ok={gate.get('short_ok')}")
            gate_parts.append(f"qqq={_fmt_num(gate.get('qqq_last'), 2)}/{_fmt_num(qqq_vwap, 2)}")
            if gate.get("qqew_last") is not None or qqew_vwap is not None:
                gate_parts.append(f"qqew={_fmt_num(gate.get('qqew_last'), 2)}/{_fmt_num(qqew_vwap, 2)}")
            gate_parts.append(f"vel={_fmt_num(velocity, 2)}")
            gate_parts.append(f"lvl={_fmt_num(level, 1)}")
            gate_parts.append(f"vel_th={_fmt_num(gate.get('add_velocity_threshold', gate.get('add_threshold')), 1)}")
            gate_parts.append(f"lvl_th={_fmt_num(gate.get('add_level_threshold', 800), 0)}")
            if strength is not None:
                gate_parts.append(f"strength={strength}")
            if strength_metric is not None:
                gate_parts.append(
                    "Strength "
                    f"({_fmt_num(strength_metric, 1)}) = max(abs(Level: {_fmt_num(level, 1)}), abs(Velocity: {_fmt_num(velocity, 1)}))"
                )
            gate_parts.append(f"atr={_fmt_num(gate.get('atr'), 3)}")
            gate_parts.append(f"sym={gate.get('exec_symbol') or '-'}")
        gate_s = "<br>".join(gate_parts)
        gate_matrix = _gate_matrix(gate)

        orders = row.get("orders") or []
        fills = row.get("fills") or []
        of = []
        if orders:
            of.append("orders=" + ",".join(sorted(set(orders))[:4]))
        if fills:
            of.append("fills=" + ",".join(sorted(set(fills))[:4]))
        order_fill = "<br>".join(of) if of else "无"

        if gate_reason == "-":
            why = "无 gate 日志"
        elif gate_reason == "already_traded":
            why = "已持仓"
        elif gate_reason == "amp_not_locked":
            why = "09:45 amp gate 未锁定"
        elif gate_reason == "minute_close_unavailable":
            why = "缺少 1m close（trend guard）"
        elif gate_reason == "trend_guard_reject":
            why = "VWAP trend guard 未通过"
        elif gate_reason == "trend_not_aligned":
            why = "QQQ/QQEW 趋势未共振"
        elif gate_reason in ("add_velocity_deadzone_for_long", "add_velocity_deadzone_for_short"):
            why = "AD-NASD 方向死区未突破（疑似微噪音）"
        elif gate_reason.startswith("add_velocity"):
            why = "AD-NASD velocity gate 未通过"
        elif gate_reason == "strength_not_ok":
            why = "Strength gate 未通过（|L|<阈值 且 |V|<阈值）"
        elif gate_reason == "data_stale":
            why = "实时数据不新鲜（>5s）"
        elif gate_reason == "add_3m_unavailable":
            why = "缺少 ADD 3分钟历史值"
        elif gate_reason == "stop_reentry_cooldown":
            why = "止损后同向冷却中"
        elif gate_reason == "deviation_below_threshold":
            why = "QQQ-VWAP deviation 不足"
        elif gate_reason == "atr_unavailable_at_entry":
            why = "ATR 不可用"
        elif gate_reason == "qty_zero":
            why = "qty 为 0"
        else:
            why = gate_reason

        lines.append(f"| {minute} | {data_status} | {gate_s} | {gate_matrix} | {order_fill} | {why} |")

    return lines


def _build_report(
    day: str,
    strategy_id: str,
    log_path: Path,
    diag_path: Path,
    state_path: Path,
    obs_path: Path,
    trader_rows: list[Row],
    diag_rows: list[Row],
    state: Optional[dict[str, Any]],
    obs: Optional[dict[str, Any]],
    from_hm: str,
    to_hm: str,
) -> str:
    all_rows = sorted([*trader_rows, *diag_rows], key=lambda r: r.ts)
    event_counts = Counter(r.event for r in all_rows)
    blocked_reasons = Counter(
        str(r.payload.get("reason") or "") for r in diag_rows if r.event == "qqq_entry_blocked"
    )
    blocked_reasons.pop("", None)
    reason_first_ts: dict[str, str] = {}
    reason_minute_set: dict[str, set[str]] = {}
    for r in diag_rows:
        if r.event != "qqq_entry_blocked":
            continue
        reason = str(r.payload.get("reason") or "")
        if not reason:
            continue
        if reason not in reason_first_ts:
            reason_first_ts[reason] = r.ts
        reason_minute_set.setdefault(reason, set()).add(_minute_key(r.ts))
    missing_payloads = Counter(
        json.dumps(r.payload.get("missing", []), ensure_ascii=False)
        for r in trader_rows
        if r.event == "qqq_required_data_missing(debug)"
    )
    hb = _analyze_heartbeat(diag_rows)
    redis_timeout_n = sum(1 for r in trader_rows if r.event == "SFS-1s Redis write timed out")
    code_versions = sorted(
        {
            str(r.payload.get("code_version"))
            for r in diag_rows
            if r.payload.get("code_version")
        }
    )

    first_init = _pick_first(all_rows, "qqq_intraday_initialized")
    first_amp = _pick_first(all_rows, "qqq_amp_locked")
    first_enter = _pick_first(all_rows, "qqq_entered")
    last_shutdown = _pick_last(all_rows, "qqq_intraday_shutdown")
    traded = bool(first_enter is not None)
    if state is not None:
        traded = traded or bool(state.get("is_traded", False))

    runs = _build_runs(trader_rows)
    minute_timeline = _build_minute_timeline(diag_rows, trader_rows)
    order_fill_rows = _infer_order_fill_rows(trader_rows)

    lines: list[str] = []
    lines.append(f"# QQQ 日报 - {day}")
    lines.append("")
    lines.append("## 执行摘要")
    lines.append(f"- strategy_id: `{strategy_id}`")
    lines.append(f"- traded: `{traded}`")
    lines.append(f"- first_initialized: `{first_init.ts if first_init else '-'}`")
    lines.append(f"- first_amp_locked: `{first_amp.ts if first_amp else '-'}`")
    lines.append(f"- first_entered: `{first_enter.ts if first_enter else '-'}`")
    lines.append(f"- last_shutdown: `{last_shutdown.ts if last_shutdown else '-'}`")
    lines.append(f"- source_rows: `{len(all_rows)}` (trader={len(trader_rows)}, diag={len(diag_rows)})")
    lines.append(f"- report_window_et: `{from_hm}-{to_hm}`")
    lines.append(f"- order_or_fill_rows_found: `{len(order_fill_rows)}`")
    lines.append("")

    lines.append("## 交易结论")
    if traded:
        lines.append("- verdict: `today did trade`（检测到 `qqq_entered` 或 state `is_traded=true`）")
    else:
        lines.append("- verdict: `today no trade`（未检测到 `qqq_entered`，且无关联 order/fill）")
    if blocked_reasons:
        top_reason, top_n = blocked_reasons.most_common(1)[0]
        lines.append(f"- dominant_gate_block: `{top_reason}` x `{top_n}`")

    amp_not_locked_n = blocked_reasons.get("amp_not_locked", 0)
    if amp_not_locked_n > 0:
        lines.append("- diagnosis: 观察窗口内 `amp_locked` gate 未通过")
        if runs:
            run_notes = []
            for i, run in enumerate(runs, start=1):
                run_notes.append(
                    f"run{i}[{_dt(run['start']).strftime('%H:%M:%S')}]:"
                    f" base={run['price_base_locked']}, 0945={run['price_0945_locked']}, amp={run['amp_locked']}"
                )
            lines.append(f"- run_gate_summary: {'; '.join(run_notes)}")
            if any((not r["price_base_locked"]) and r["price_0945_locked"] for r in runs):
                lines.append(
                    "- likely_root_cause: 策略在 09:30 后重启，拿到 `price_0945` 但错过 09:30:00-09:30:10 的 price_base 窗口，后续无法锁定 amp"
                )
    lines.append("")

    lines.append("## 输入源")
    lines.append(f"- trader_log: `{log_path}`")
    lines.append(f"- diag_log: `{diag_path}`")
    lines.append(f"- state_file: `{state_path}`")
    lines.append(f"- observability_file: `{obs_path}`")
    lines.append("")

    lines.append("## 运行分段")
    if runs:
        lines.append("| Run | 开始 | 结束 | Base Locked | 09:45 Locked | Amp Locked | Entered | Top Block |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, run in enumerate(runs, start=1):
            top_block = "-"
            if run["entry_blocked_reasons"]:
                b, n = run["entry_blocked_reasons"].most_common(1)[0]
                top_block = f"{b} x{n}"
            lines.append(
                f"| {i} | {_dt(run['start']).strftime('%H:%M:%S')} | {_dt(run['end']).strftime('%H:%M:%S')} | "
                f"{run['price_base_locked']} | {run['price_0945_locked']} | {run['amp_locked']} | {run['entered']} | {top_block} |"
            )
    else:
        lines.append("- 未找到策略运行分段")
    lines.append("")

    lines.append("## 分钟级 Gate/Order 时间线")
    if minute_timeline:
        lines.extend(_render_minute_table(minute_timeline, limit=240))
    else:
        lines.append("- 无分钟级时间线数据")
    lines.append("")

    lines.append("## 订单生命周期")
    lifecycle = {
        "intent_submitted": [r for r in order_fill_rows if "intent" in r.event.lower() or "submit" in r.event.lower()],
        "broker_ack": [r for r in order_fill_rows if "ack" in r.event.lower() or "submitted" in r.event.lower()],
        "partial_fill": [r for r in order_fill_rows if "partial" in r.event.lower()],
        "full_fill": [r for r in order_fill_rows if "fill" in r.event.lower() and "partial" not in r.event.lower()],
        "flatten": [r for r in order_fill_rows if "flatten" in r.event.lower()],
    }
    lines.append("| Stage | Count | First | Last |")
    lines.append("| --- | --- | --- | --- |")
    for stage, rs in lifecycle.items():
        if not rs:
            lines.append(f"| {stage} | 0 | - | - |")
        else:
            lines.append(f"| {stage} | {len(rs)} | {rs[0].ts} | {rs[-1].ts} |")
    lines.append("")

    lines.append("## 入场 Gate 诊断")
    if blocked_reasons:
        for reason, n in blocked_reasons.most_common():
            lines.append(f"- `{reason}`: {n}")
    else:
        lines.append("- 当日无 `qqq_entry_blocked` 记录")
    lines.append("")

    lines.append("## Gate失败 Top-N（分钟聚合）")
    if blocked_reasons:
        lines.append("| Reason | Minutes | Blocks | First Seen (ET) |")
        lines.append("| --- | --- | --- | --- |")
        for reason, blocks in blocked_reasons.most_common(10):
            mins = len(reason_minute_set.get(reason, set()))
            first_seen = reason_first_ts.get(reason, "-")
            lines.append(f"| {reason} | {mins} | {blocks} | {first_seen} |")
    else:
        lines.append("- 无 Gate 失败")
    lines.append("")

    lines.append("## 数据与基础设施信号")
    if missing_payloads:
        lines.append("- required_data_missing(debug) 高频 payload：")
        for payload, n in missing_payloads.most_common(8):
            lines.append(f"- `{payload}`: {n}")
    else:
        lines.append("- 未发现 `qqq_required_data_missing(debug)`")
    lines.append(f"- sfs_1s_redis_timeout_count: `{redis_timeout_n}`")
    lines.append("")

    lines.append("## 循环健康度")
    lines.append(f"- heartbeat_count: `{hb['count']}`")
    lines.append(f"- heartbeat_max_gap_sec: `{hb['max_gap_sec']}`")
    lines.append(f"- heartbeat_gap_over_90s: `{hb['gap_over_90s']}`")
    lines.append(f"- code_versions_seen: `{', '.join(code_versions) if code_versions else '-'}`")
    lines.append("")

    lines.append("## 最终快照")
    if state is None:
        lines.append("- state: 缺失")
    else:
        lines.append(f"- state.day: `{state.get('day')}`")
        lines.append(f"- state.is_traded: `{state.get('is_traded')}`")
        lines.append(f"- state.amp_locked: `{state.get('amp_locked')}`")
        lines.append(f"- state.price_base: `{state.get('price_base')}`")
        lines.append(f"- state.price_0945: `{state.get('price_0945')}`")
        lines.append(f"- state.locked_target(amp): `{state.get('locked_target')}`")
        lines.append(f"- state.trade_target_px: `{state.get('trade_target_px')}`")
        lines.append(f"- state.updated_at: `{state.get('updated_at')}`")
        lines.append(f"- state.updated_at_semantics: `{state.get('updated_at_semantics') or 'legacy_unspecified'}`")
        lines.append(f"- state.last_state_change_at: `{state.get('last_state_change_at') or state.get('updated_at')}`")
        lines.append(f"- state.reason: `{state.get('reason')}`")
    if obs is None:
        lines.append("- observability: 缺失")
    else:
        lines.append(f"- obs.ts_now_et: `{obs.get('ts_now_et')}`")
        lines.append(f"- obs.ready: `{obs.get('ready')}`")
        lines.append(f"- obs.missing_required: `{obs.get('missing_required')}`")
    lines.append("")

    lines.append("## 事件计数 Top")
    for event, n in event_counts.most_common(25):
        lines.append(f"- `{event}`: {n}")
    lines.append("")

    lines.append("## 关键时间点")
    for key in KEY_EVENTS:
        first = _pick_first(all_rows, key)
        if first is None:
            continue
        lines.append(f"- {key}: `{first.ts}` ({first.source})")
    if first_init and first_amp:
        lines.append(
            f"- delta_initialized_to_amp_locked_sec: `{round((_dt(first_amp.ts) - _dt(first_init.ts)).total_seconds(), 3)}`"
        )
    lines.append("")

    lines.append("## 最近 20 条时间线")
    for r in all_rows[-20:]:
        reason = r.payload.get("reason")
        reason_s = f" reason={reason}" if reason else ""
        lines.append(f"- `{r.ts}` [{r.source}] `{r.event}`{reason_s}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    day = args.day
    strategy_id = args.strategy_id

    state_dir = Path(args.state_dir).expanduser()
    log_path = Path(args.log_path).expanduser()
    diag_path = state_dir / f"qqq_intraday_diag_{strategy_id}.jsonl"
    state_path = state_dir / f"qqq_intraday_{strategy_id}.json"
    obs_path = state_dir / f"qqq_intraday_observability_{strategy_id}.json"

    trader_rows = _load_trader_rows(log_path, day, strategy_id, args.from_hm, args.to_hm)
    diag_rows = _load_diag_rows(diag_path, day, strategy_id, args.from_hm, args.to_hm)
    state = _load_json(state_path)
    obs = _load_json(obs_path)

    report = _build_report(
        day=day,
        strategy_id=strategy_id,
        log_path=log_path,
        diag_path=diag_path,
        state_path=state_path,
        obs_path=obs_path,
        trader_rows=trader_rows,
        diag_rows=diag_rows,
        state=state,
        obs=obs,
        from_hm=args.from_hm,
        to_hm=args.to_hm,
    )

    out = args.out
    if not out:
        out_dir = Path("runs/reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = str(out_dir / f"qqq_daily_report_{strategy_id}_{day}.md")
    out_path = Path(out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    if args.stdout:
        print(report)
    else:
        print(f"QQQ report written to: {out_path}")


if __name__ == "__main__":
    main()
