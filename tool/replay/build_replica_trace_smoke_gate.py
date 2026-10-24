#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_SYSTEM_STAGES = ("strategy_started", "strategy_ready")
DEFAULT_DECISION_STAGES = ("gate_evaluated", "entry_gate_blocked", "entry_intent_requested")


def _iter_day_audit_files(audit_root: Path, day: str) -> List[Path]:
    day_dir = audit_root / str(day)
    if not day_dir.exists():
        return []
    return sorted(day_dir.glob("*.jsonl"))


def _load_strategy_rows(audit_root: Path, *, day: str, strategy_id: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in _iter_day_audit_files(audit_root, day):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if str(row.get("strategy_id") or "").strip() != str(strategy_id or "").strip():
                    continue
                rows.append(row)
        except Exception:
            continue
    rows.sort(key=lambda item: str(item.get("ts") or ""))
    return rows


def build_replica_trace_smoke_gate(
    *,
    audit_root: Path,
    day: str,
    strategy_id: str,
    required_system_stages: Iterable[str] = DEFAULT_SYSTEM_STAGES,
    required_decision_stages: Iterable[str] = DEFAULT_DECISION_STAGES,
) -> Dict[str, Any]:
    system_stage_set = {str(item).strip() for item in required_system_stages if str(item).strip()}
    decision_stage_set = {str(item).strip() for item in required_decision_stages if str(item).strip()}
    rows = _load_strategy_rows(audit_root, day=str(day), strategy_id=str(strategy_id))

    observed_stages = [str(row.get("stage") or "").strip() for row in rows if str(row.get("stage") or "").strip()]
    stage_counts = Counter(observed_stages)
    observed_stage_set = set(observed_stages)

    system_checks = {stage: (stage in observed_stage_set) for stage in sorted(system_stage_set)}
    decision_checks = {stage: (stage in observed_stage_set) for stage in sorted(decision_stage_set)}
    decision_chain_seen = any(decision_checks.values())

    trace_ids = sorted({str(row.get("trace_id") or "").strip() for row in rows if str(row.get("trace_id") or "").strip()})
    decision_rows = [row for row in rows if str(row.get("stage") or "").strip() in decision_stage_set]
    decision_symbols = sorted({str(row.get("symbol") or "").strip().upper() for row in decision_rows if str(row.get("symbol") or "").strip()})

    report = {
        "audit_root": str(audit_root),
        "day": str(day),
        "strategy_id": str(strategy_id),
        "required_system_stages": sorted(system_stage_set),
        "required_decision_stages": sorted(decision_stage_set),
        "checks": {
            "system_stages_present": all(system_checks.values()) if system_checks else True,
            "decision_chain_stage_present": bool(decision_chain_seen),
        },
        "observed": {
            "row_count": len(rows),
            "trace_count": len(trace_ids),
            "trace_ids": trace_ids,
            "decision_symbol_count": len(decision_symbols),
            "decision_symbols": decision_symbols,
            "stage_counts": dict(sorted(stage_counts.items())),
            "system_stage_presence": system_checks,
            "decision_stage_presence": decision_checks,
        },
        "ready_for_trace_parity_capture": (all(system_checks.values()) if system_checks else True) and bool(decision_chain_seen),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build trace smoke gate for replica strategy audit readiness.")
    parser.add_argument("--day", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--strategy-id", default="index_super_win_001")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--required-system-stage", action="append", default=[])
    parser.add_argument("--required-decision-stage", action="append", default=[])
    args = parser.parse_args()

    required_system = tuple(args.required_system_stage) if args.required_system_stage else DEFAULT_SYSTEM_STAGES
    required_decision = tuple(args.required_decision_stage) if args.required_decision_stage else DEFAULT_DECISION_STAGES
    report = build_replica_trace_smoke_gate(
        audit_root=Path(args.audit_root),
        day=str(args.day),
        strategy_id=str(args.strategy_id),
        required_system_stages=required_system,
        required_decision_stages=required_decision,
    )
    output_path = Path(args.output_path) if str(args.output_path).strip() else ROOT / "runtime" / "state" / "index_super_win_trace_smoke_gate.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(output_path)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("ready_for_trace_parity_capture")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
