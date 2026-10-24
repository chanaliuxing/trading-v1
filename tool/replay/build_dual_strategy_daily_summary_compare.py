#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ENTRY_SUBMIT_STAGES = {"entry_intent_submitted"}
REPLICA_STOP_REBUILD_STAGES = {"protection_audit_self_heal", "protection_audit_recovered", "protective_stop_live"}
QQQ_STOP_REBUILD_STAGES = {"protective_stop_submitted", "stop_audit_self_heal"}
FLATTEN_STAGES = {"flatten_requested", "flatten_submitted", "flatten_confirmed", "flatten_terminal", "protective_stop_filled"}


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    payload = _load_json(path)
    events = list(payload.get("events") or [])
    return [dict(item) for item in events if isinstance(item, dict)]


def _parse_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _find_first(events: Iterable[Dict[str, Any]], *, stages: set[str]) -> Optional[Dict[str, Any]]:
    for event in events:
        if str(event.get("stage") or "").strip() in stages:
            return event
    return None


def _find_flatten_event(events: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for event in events:
        stage = str(event.get("stage") or "").strip()
        reason = str(event.get("reason") or "").strip()
        if stage in FLATTEN_STAGES:
            return event
        if "flatten" in reason.lower() or reason in {"logic_retreat", "stop_hit", "tp1_then_trailing_stop", "tp1_then_eod", "STOP_LOSS_MISSING_KILL_SWITCH"}:
            return event
    return None


def _extract_reason(event: Optional[Dict[str, Any]]) -> Optional[str]:
    if not event:
        return None
    reason = str(event.get("reason") or "").strip()
    if reason:
        return reason
    payload = dict(event.get("payload") or {})
    reason = str(payload.get("reason") or "").strip()
    return reason or None


def _has_stop_rebuild(events: Iterable[Dict[str, Any]], *, replica: bool) -> bool:
    stages = REPLICA_STOP_REBUILD_STAGES if replica else QQQ_STOP_REBUILD_STAGES
    for event in events:
        stage = str(event.get("stage") or "").strip()
        reason = str(event.get("reason") or "").strip().lower()
        if stage in stages:
            return True
        if not replica and reason in {"audit_self_heal", "position_reconcile_protection", "rebuild_stop_after_restart"}:
            return True
        if replica and ("self_heal" in reason or "reconcile_protection" in reason or "restart_protective_stop" in reason):
            return True
    return False


def _timing_delta_seconds(sample_ts: Optional[datetime], qqq_events: List[Dict[str, Any]], replica_events: List[Dict[str, Any]]) -> Optional[float]:
    if sample_ts is None:
        return None
    qqq = _find_first(qqq_events, stages=ENTRY_SUBMIT_STAGES)
    replica = _find_first(replica_events, stages=ENTRY_SUBMIT_STAGES)
    qqq_ts = _parse_ts((qqq or {}).get("ts"))
    replica_ts = _parse_ts((replica or {}).get("ts"))
    if qqq_ts is None or replica_ts is None:
        return None
    return abs((replica_ts - qqq_ts).total_seconds())


def build_dual_strategy_daily_summary_compare(
    *,
    sample_dir: Path,
    qqq_trace_dir: Path,
    replica_trace_dir: Path,
    output_dir: Path,
    submit_timing_tolerance_sec: float = 15.0,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_paths = sorted(
        [path for path in sample_dir.glob("*.json") if path.name not in {"session_report.json", "session_manifest.json"}],
        key=lambda p: p.name,
    )

    rows: List[Dict[str, Any]] = []
    gate_ok = 0
    gate_mismatch = 0
    submit_timing_ok = 0
    submit_timing_mismatch = 0
    flatten_ok = 0
    flatten_mismatch = 0
    stop_rebuild_ok = 0
    stop_rebuild_mismatch = 0
    gate_counter = Counter()
    flatten_counter = Counter()

    for sample_path in sample_paths:
        sample = _load_json(sample_path)
        summary = dict(sample.get("summary") or {})
        symbol = str(sample.get("symbol") or "").strip().upper()
        stem = sample_path.stem
        qqq_events = _load_events(qqq_trace_dir / f"{stem}.json")
        replica_events = _load_events(replica_trace_dir / f"{stem}.json")
        sample_ts = _parse_ts(sample.get("captured_at_et"))

        expected_gate_reason = str(summary.get("index_gate_reason") or "").strip() or None
        replica_gate_reason = _extract_reason(_find_first(replica_events, stages={"entry_gate_blocked"}))
        gate_status = "not_applicable"
        if expected_gate_reason or replica_gate_reason:
            gate_counter[str(expected_gate_reason or replica_gate_reason or "unknown")] += 1
            gate_status = "ok" if expected_gate_reason == replica_gate_reason else "mismatch"
            if gate_status == "ok":
                gate_ok += 1
            else:
                gate_mismatch += 1

        submit_delta = _timing_delta_seconds(sample_ts, qqq_events, replica_events)
        submit_status = "not_applicable"
        if submit_delta is not None:
            submit_status = "ok" if submit_delta <= float(submit_timing_tolerance_sec) else "mismatch"
            if submit_status == "ok":
                submit_timing_ok += 1
            else:
                submit_timing_mismatch += 1

        qqq_flatten_reason = _extract_reason(_find_flatten_event(qqq_events))
        replica_flatten_reason = _extract_reason(_find_flatten_event(replica_events))
        flatten_status = "not_applicable"
        if qqq_flatten_reason or replica_flatten_reason:
            flatten_counter[str(qqq_flatten_reason or replica_flatten_reason or "unknown")] += 1
            flatten_status = "ok" if qqq_flatten_reason == replica_flatten_reason else "mismatch"
            if flatten_status == "ok":
                flatten_ok += 1
            else:
                flatten_mismatch += 1

        qqq_stop_rebuild = _has_stop_rebuild(qqq_events, replica=False)
        replica_stop_rebuild = _has_stop_rebuild(replica_events, replica=True)
        stop_rebuild_status = "ok" if qqq_stop_rebuild == replica_stop_rebuild else "mismatch"
        if stop_rebuild_status == "ok":
            stop_rebuild_ok += 1
        else:
            stop_rebuild_mismatch += 1

        rows.append(
            {
                "sample_path": str(sample_path),
                "symbol": symbol,
                "captured_at_et": sample.get("captured_at_et"),
                "expected_gate_reason": expected_gate_reason,
                "replica_gate_reason": replica_gate_reason,
                "gate_reason_status": gate_status,
                "submit_timing_delta_sec": submit_delta,
                "submit_timing_status": submit_status,
                "qqq_flatten_reason": qqq_flatten_reason,
                "replica_flatten_reason": replica_flatten_reason,
                "flatten_reason_status": flatten_status,
                "qqq_stop_rebuild_seen": qqq_stop_rebuild,
                "replica_stop_rebuild_seen": replica_stop_rebuild,
                "stop_rebuild_status": stop_rebuild_status,
            }
        )

    report = {
        "sample_dir": str(sample_dir),
        "qqq_trace_dir": str(qqq_trace_dir),
        "replica_trace_dir": str(replica_trace_dir),
        "sample_count": len(rows),
        "submit_timing_tolerance_sec": float(submit_timing_tolerance_sec),
        "gate_reason": {"ok": gate_ok, "mismatch": gate_mismatch, "top_expected_or_actual": dict(gate_counter.most_common(10))},
        "submit_timing": {"ok": submit_timing_ok, "mismatch": submit_timing_mismatch},
        "flatten_reason": {"ok": flatten_ok, "mismatch": flatten_mismatch, "top_expected_or_actual": dict(flatten_counter.most_common(10))},
        "stop_rebuild": {"ok": stop_rebuild_ok, "mismatch": stop_rebuild_mismatch},
        "summary_compare_green": gate_mismatch == 0 and submit_timing_mismatch == 0 and flatten_mismatch == 0 and stop_rebuild_mismatch == 0,
        "rows": rows,
    }
    report_path = output_dir / "dual_strategy_daily_summary_compare.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build daily summary comparison for qqq vs replica same-symbol proof")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--qqq-trace-dir", required=True)
    parser.add_argument("--replica-trace-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--submit-timing-tolerance-sec", type=float, default=15.0)
    args = parser.parse_args()

    report = build_dual_strategy_daily_summary_compare(
        sample_dir=Path(args.sample_dir),
        qqq_trace_dir=Path(args.qqq_trace_dir),
        replica_trace_dir=Path(args.replica_trace_dir),
        output_dir=Path(args.output_dir),
        submit_timing_tolerance_sec=float(args.submit_timing_tolerance_sec),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("summary_compare_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
