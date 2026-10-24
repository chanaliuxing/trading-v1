#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REPLICA_FORBIDDEN_STAGES_ON_BLOCKED = {
    "entry_intent_submitted",
    "entry_confirmed",
    "flatten_requested",
    "flatten_submitted",
    "protective_stop_live",
    "protection_audit_self_heal",
    "protection_audit_recovered",
}
REPLICA_FORBIDDEN_REASONS_ON_BLOCKED = {
    "logic_retreat",
    "tp1_hit",
    "tp1_then_trailing_stop",
    "tp1_then_eod",
    "force_flatten_1559_EOD",
    "stop_hit",
    "target_hit",
    "breakeven_shield",
    "stop_widened",
    "qqq_intraday_add",
    "STOP_LOSS_MISSING_KILL_SWITCH",
}
HIGH_RISK_AUDIT_STAGES = {
    "fill_attribution_ambiguous",
    "fill_attribution_fallback_broker_order_id",
    "cancelled_broker_ghost_order",
}
REPLICA_LIVE_EXIT_STAGES = {
    "entry_confirmed",
    "tp1_exit_confirmed",
    "flatten_confirmed",
    "protective_stop_filled",
}
REPLICA_OWNERSHIP_EVIDENCE_STAGES = {
    "ownership_entry_lot_live",
    "ownership_exit_allocated",
}


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    payload = _load_json(path)
    return [dict(item) for item in list(payload.get("events") or []) if isinstance(item, dict)]


def _iter_audit_rows(audit_root: Path, day: str) -> Iterable[Dict[str, Any]]:
    day_dir = audit_root / day
    if not day_dir.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(day_dir.glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = str(line or "").strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
        except Exception:
            continue
    return rows


def build_dual_strategy_foreign_safety_report(
    *,
    sample_dir: Path,
    qqq_trace_dir: Path,
    replica_trace_dir: Path,
    audit_root: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_paths = sorted(
        [path for path in sample_dir.glob("*.json") if path.name not in {"session_report.json", "session_manifest.json"}],
        key=lambda p: p.name,
    )

    blocked_rows: List[Dict[str, Any]] = []
    blocked_with_forbidden = 0
    ownership_rows: List[Dict[str, Any]] = []
    samples_missing_ownership_evidence = 0

    for sample_path in sample_paths:
        sample = _load_json(sample_path)
        summary = dict(sample.get("summary") or {})
        expected_gate_reason = str(summary.get("index_gate_reason") or "").strip()
        final_state = str(summary.get("index_final_state") or "").strip().lower()
        replica_events = _load_events(replica_trace_dir / f"{sample_path.stem}.json")
        action_events = [
            {
                "stage": str(event.get("stage") or "").strip(),
                "reason": str(event.get("reason") or "").strip(),
                "ts": event.get("ts"),
            }
            for event in replica_events
            if str(event.get("stage") or "").strip() in REPLICA_LIVE_EXIT_STAGES
        ]
        ownership_events = [
            {
                "stage": str(event.get("stage") or "").strip(),
                "reason": str(event.get("reason") or "").strip(),
                "ts": event.get("ts"),
            }
            for event in replica_events
            if str(event.get("stage") or "").strip() in REPLICA_OWNERSHIP_EVIDENCE_STAGES
        ]
        ownership_ok = (not action_events) or bool(ownership_events)
        if not ownership_ok:
            samples_missing_ownership_evidence += 1
        ownership_rows.append(
            {
                "sample_path": str(sample_path),
                "symbol": str(sample.get("symbol") or "").strip().upper(),
                "final_state": final_state or None,
                "action_events": action_events,
                "ownership_events": ownership_events,
                "ownership_evidence_ok": ownership_ok,
            }
        )
        if final_state != "blocked" and not expected_gate_reason:
            continue
        forbidden = []
        for event in replica_events:
            stage = str(event.get("stage") or "").strip()
            reason = str(event.get("reason") or "").strip()
            if stage in REPLICA_FORBIDDEN_STAGES_ON_BLOCKED or reason in REPLICA_FORBIDDEN_REASONS_ON_BLOCKED:
                forbidden.append({"stage": stage, "reason": reason, "ts": event.get("ts")})
        row = {
            "sample_path": str(sample_path),
            "symbol": str(sample.get("symbol") or "").strip().upper(),
            "expected_gate_reason": expected_gate_reason or None,
            "final_state": final_state,
            "forbidden_events": forbidden,
            "ok": len(forbidden) == 0,
        }
        if forbidden:
            blocked_with_forbidden += 1
        blocked_rows.append(row)

    days = sorted({str(_load_json(p).get("captured_at_et") or "")[:10] for p in sample_paths if str(_load_json(p).get("captured_at_et") or "")[:10]})
    audit_stage_rows: List[Dict[str, Any]] = []
    ambiguous_count = 0
    fallback_count = 0
    ghost_cancel_count = 0
    for day in days:
        for row in _iter_audit_rows(audit_root, day):
            stage = str(row.get("stage") or "").strip()
            if stage not in HIGH_RISK_AUDIT_STAGES:
                continue
            item = {
                "day": day,
                "ts": row.get("ts"),
                "stage": stage,
                "strategy_id": row.get("strategy_id"),
                "symbol": str(row.get("symbol") or "").strip().upper() or None,
                "reason": row.get("reason"),
            }
            audit_stage_rows.append(item)
            if stage == "fill_attribution_ambiguous":
                ambiguous_count += 1
            elif stage == "fill_attribution_fallback_broker_order_id":
                fallback_count += 1
            elif stage == "cancelled_broker_ghost_order":
                ghost_cancel_count += 1

    report = {
        "sample_dir": str(sample_dir),
        "qqq_trace_dir": str(qqq_trace_dir),
        "replica_trace_dir": str(replica_trace_dir),
        "audit_root": str(audit_root),
        "blocked_sample_count": len(blocked_rows),
        "blocked_samples_with_forbidden_replica_actions": blocked_with_forbidden,
        "blocked_samples_ok": blocked_with_forbidden == 0,
        "samples_missing_ownership_evidence": samples_missing_ownership_evidence,
        "audit_high_risk_counts": {
            "fill_attribution_ambiguous": ambiguous_count,
            "fill_attribution_fallback_broker_order_id": fallback_count,
            "cancelled_broker_ghost_order": ghost_cancel_count,
        },
        "foreign_safety_green": (
            blocked_with_forbidden == 0
            and ambiguous_count == 0
            and fallback_count == 0
            and ghost_cancel_count == 0
            and samples_missing_ownership_evidence == 0
        ),
        "blocked_rows": blocked_rows,
        "ownership_rows": ownership_rows,
        "audit_high_risk_rows": audit_stage_rows,
    }
    report_path = output_dir / "dual_strategy_foreign_safety_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build foreign-safety report for dual-strategy same-symbol proof")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--qqq-trace-dir", required=True)
    parser.add_argument("--replica-trace-dir", required=True)
    parser.add_argument("--audit-root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    report = build_dual_strategy_foreign_safety_report(
        sample_dir=Path(args.sample_dir),
        qqq_trace_dir=Path(args.qqq_trace_dir),
        replica_trace_dir=Path(args.replica_trace_dir),
        audit_root=Path(args.audit_root),
        output_dir=Path(args.output_dir),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("foreign_safety_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
