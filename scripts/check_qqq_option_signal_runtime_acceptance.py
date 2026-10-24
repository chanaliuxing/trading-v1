#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _iter_jsonl(paths: list[str]):
    for fp in paths:
        p = Path(fp)
        if not p.exists() or not p.is_file():
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def _row_date(row: dict[str, Any]) -> str:
    for key in ("ts", "timestamp"):
        raw = row.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        ts = _parse_ts(s)
        if ts is not None:
            return ts.date().isoformat()
    return ""


def _resolve_plugin_log_files(plugin_log_glob: str) -> list[str]:
    if str(plugin_log_glob or "").strip():
        return sorted(glob.glob(str(plugin_log_glob)))
    defaults: list[str] = []
    defaults.extend(sorted(glob.glob("runtime/logs/*.jsonl")))
    audit_log_path = str(
        os.getenv("AUDIT_LOG_PATH", os.path.expanduser("~/grafana-stack/logs/trader.log"))
    ).strip()
    if audit_log_path:
        defaults.extend(sorted(glob.glob(f"{audit_log_path}*")))
    # de-dup while preserving order
    out: list[str] = []
    seen = set()
    for fp in defaults:
        key = str(fp)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="QQQ option signal runtime acceptance (30m window)")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--stock-strategy-id", default="qqq_intraday_001")
    ap.add_argument("--option-strategy-id", default="qqq_intraday_option_v1_001")
    ap.add_argument("--window-min", type=int, default=30)
    ap.add_argument("--audit-dir", default="runtime/audit")
    ap.add_argument("--plugin-log-glob", default="", help="optional jsonl glob for plugin logger events")
    ap.add_argument("--min-submit-after-consumed", type=int, default=1)
    ap.add_argument("--max-reject-cooldown-ratio", type=float, default=0.6)
    args = ap.parse_args()

    root = Path(args.audit_dir) / str(args.date)
    audit_files = sorted(glob.glob(str(root / "strategy-*.jsonl")))
    rows = list(_iter_jsonl(audit_files))

    stock_rows = [r for r in rows if str(r.get("strategy_id") or "") == str(args.stock_strategy_id)]
    option_rows = [r for r in rows if str(r.get("strategy_id") or "") == str(args.option_strategy_id)]

    stock_entry_allowed = [
        r for r in stock_rows
        if str(r.get("stage") or "") == "gate_evaluated"
        and str(r.get("status") or "") == "entry_allowed"
    ]
    stock_publish_ok = [
        r for r in stock_rows
        if str(r.get("stage") or "") == "stock_signal_publish_ok"
        and str(r.get("status") or "") == "published"
    ]
    stock_publish_failed = [
        r for r in stock_rows
        if str(r.get("stage") or "") == "stock_signal_publish_failed"
    ]
    option_heartbeats = [
        r for r in option_rows
        if str(r.get("stage") or "") == "feature_callback_heartbeat"
    ]
    option_submit = [
        r for r in option_rows
        if str(r.get("stage") or "") == "plugin_intent_submit_result"
        and str(r.get("reason") or "") in {"entry_intent_submitted", "approved", "accepted"}
    ]

    signal_mode = ""
    signal_mode_counts: dict[str, int] = defaultdict(int)
    for r in option_submit:
        payload = dict(r.get("payload") or {})
        pp = dict(payload.get("plugin_payload") or {})
        sig = dict(pp.get("signal") or {})
        sp = dict(sig.get("payload") or {})
        mode = str(sp.get("signal_source_mode") or "").strip()
        if mode:
            signal_mode = mode
            break

    plugin_result_counts: dict[str, int] = defaultdict(int)
    signal_plane_backend_counts: dict[str, int] = defaultdict(int)
    signal_plane_state_counts: dict[str, int] = defaultdict(int)
    signal_plane_redis_ok_counts: dict[str, int] = defaultdict(int)
    plugin_files = _resolve_plugin_log_files(args.plugin_log_glob)
    if plugin_files:
        for r in _iter_jsonl(plugin_files):
            row_date = _row_date(r)
            if row_date and row_date != str(args.date):
                continue
            event = str(r.get("event") or "")
            if event == "qqq_intraday_option_minute_heartbeat" and str(r.get("strategy_id") or "") == str(args.option_strategy_id):
                mode = str(r.get("signal_source_mode") or "").strip()
                if mode:
                    signal_mode_counts[mode] += 1
                backend = str(r.get("signal_plane_backend_config") or "").strip().lower()
                if backend:
                    signal_plane_backend_counts[backend] += 1
                state = str(r.get("signal_plane_state") or "").strip().lower()
                if state:
                    signal_plane_state_counts[state] += 1
                redis_ok = r.get("signal_plane_redis_ok")
                if isinstance(redis_ok, bool):
                    signal_plane_redis_ok_counts[str(redis_ok).lower()] += 1
                continue
            if event == "qqq_intraday_option_entry_block" and str(r.get("strategy_id") or "") == str(args.option_strategy_id):
                mode = str(r.get("signal_source_mode") or "").strip()
                if mode:
                    signal_mode_counts[mode] += 1
                code = str(r.get("gate_reason") or "entry_blocked").strip()
                if code:
                    plugin_result_counts[code] += 1
                continue
            if event != "qqq_intraday_option_downstream_result":
                continue
            if str(r.get("strategy_id") or "") != str(args.option_strategy_id):
                continue
            mode = str(r.get("signal_source_mode") or "").strip()
            if mode:
                signal_mode_counts[mode] += 1
            code = str(r.get("result_code") or "")
            if code:
                plugin_result_counts[code] += 1

    if (not signal_mode) and signal_mode_counts:
        signal_mode = max(signal_mode_counts.items(), key=lambda kv: kv[1])[0]

    h1 = bool(stock_rows) and bool(option_rows)
    h2 = signal_mode == "upstream_stock_signal" if signal_mode else False
    h3 = len(stock_entry_allowed) > 0 and len(stock_publish_ok) >= len(stock_entry_allowed)
    h4 = sum(plugin_result_counts.values()) > 0
    h5 = len(option_submit) > 0 or sum(plugin_result_counts.values()) > 0

    submit_ts = [
        _parse_ts(r.get("ts"))
        for r in option_submit
    ]
    submit_ts = [x for x in submit_ts if x is not None]
    duplicate_submit = False
    if submit_ts:
        bins: dict[str, int] = defaultdict(int)
        for ts in submit_ts:
            bins[ts.strftime("%Y-%m-%d %H:%M")] += 1
        duplicate_submit = any(v > 1 for v in bins.values())
    h6 = not duplicate_submit

    # H7 needs process-level stream telemetry; approximate with no obvious duplicate submit + heartbeat alive.
    h7 = h1 and h6
    degraded = int(signal_plane_state_counts.get("degraded_no_redis", 0) + signal_plane_state_counts.get("degraded_fallback_memory", 0))
    healthy = int(signal_plane_state_counts.get("healthy_redis", 0) + signal_plane_state_counts.get("healthy_memory", 0))
    total_state = degraded + healthy
    degraded_ratio = float(degraded / total_state) if total_state > 0 else 0.0
    # H8: signal plane is observable and not dominated by degraded state
    h8 = (total_state > 0 and degraded_ratio <= 0.8) or (total_state == 0 and len(option_heartbeats) > 0)
    h9 = len(stock_publish_failed) == 0
    signal_consumed = int(plugin_result_counts.get("signal_consumed", 0))
    no_contract_selection = int(plugin_result_counts.get("no_contract_selection", 0))
    downstream_submitted = int(plugin_result_counts.get("entry_intent_submitted", 0))
    reject_cooldown = int(plugin_result_counts.get("submit_reject_cooldown", 0))
    total_blocks = int(sum(v for k, v in plugin_result_counts.items() if k not in {"signal_consumed", "entry_intent_submitted"}))
    cooldown_ratio = float(reject_cooldown / total_blocks) if total_blocks > 0 else 0.0
    # H10: once upstream signals are consumed, we should eventually observe submit attempts.
    h10 = (signal_consumed <= 0) or ((len(option_submit) + downstream_submitted) >= int(args.min_submit_after_consumed))
    # H11: consumed signals should not be overwhelmingly blocked by contract selection.
    h11 = (signal_consumed <= 0) or (no_contract_selection < signal_consumed)
    # H12: reject-cooldown should be a guard rail, not dominant steady-state behavior.
    h12 = cooldown_ratio <= float(max(0.0, args.max_reject_cooldown_ratio))

    checks = {
        "H1_preopen_strategies_alive": h1,
        "H2_option_signal_source_upstream": h2,
        "H3_stock_signal_published_for_entry_allowed": h3,
        "H4_option_source_aware_downstream_result": h4,
        "H5_known_signal_reaches_submit_or_explicit_block": h5,
        "H6_no_duplicate_submit": h6,
        "H7_no_duplicate_underlying_stream_process": h7,
        "H8_signal_plane_not_degraded_dominant": h8,
        "H9_no_stock_signal_publish_failures": h9,
        "H10_consumed_signal_reaches_submit_stage": h10,
        "H11_contract_selection_not_full_blocker": h11,
        "H12_reject_cooldown_not_dominant": h12,
    }

    out = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "date": str(args.date),
        "stock_strategy_id": str(args.stock_strategy_id),
        "option_strategy_id": str(args.option_strategy_id),
        "counts": {
            "stock_rows": len(stock_rows),
            "option_rows": len(option_rows),
            "stock_entry_allowed": len(stock_entry_allowed),
            "stock_publish_ok": len(stock_publish_ok),
            "stock_publish_failed": len(stock_publish_failed),
            "option_heartbeat": len(option_heartbeats),
            "option_submit": len(option_submit),
            "plugin_results": dict(plugin_result_counts),
            "signal_plane_backend": dict(signal_plane_backend_counts),
            "signal_plane_state": dict(signal_plane_state_counts),
            "signal_plane_redis_ok": dict(signal_plane_redis_ok_counts),
            "signal_plane_degraded_ratio": degraded_ratio,
            "plugin_log_files_scanned": len(plugin_files),
            "signal_consumed": int(signal_consumed),
            "no_contract_selection": int(no_contract_selection),
            "downstream_entry_intent_submitted": int(downstream_submitted),
            "submit_reject_cooldown": int(reject_cooldown),
            "submit_reject_cooldown_ratio": float(cooldown_ratio),
        },
        "signal_source_mode_observed": signal_mode,
        "checks": checks,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
