#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _looks_like_day(raw: str) -> bool:
    try:
        datetime.strptime(str(raw), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _iter_days(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted([path for path in root.iterdir() if path.is_dir() and _looks_like_day(path.name)], key=lambda p: p.name)


def _sample_paths(sample_dir: Path) -> List[Path]:
    if not sample_dir.exists():
        return []
    return sorted([p for p in sample_dir.glob("*.json") if p.name not in {"session_report.json", "session_manifest.json"}], key=lambda p: p.name)


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_real_session_proof_readiness(
    *,
    sample_root: Path,
    audit_root: Path,
    output_path: Path,
    min_total_samples: int = 1,
    min_blocked_samples: int = 1,
) -> Dict[str, Any]:
    rows = []
    ready_days = 0
    total_days = 0

    for day_dir in _iter_days(sample_root):
        total_days += 1
        sample_paths = _sample_paths(day_dir)
        blocked_samples = 0
        qqq_expected_samples = 0
        for sample_path in sample_paths:
            sample = _load_json(sample_path)
            summary = dict(sample.get("summary") or {})
            if str(summary.get("index_final_state") or "").strip().lower() == "blocked" or str(summary.get("index_gate_reason") or "").strip():
                blocked_samples += 1
            symbol = str(sample.get("symbol") or "").strip().upper()
            if bool(summary.get("qqq_is_traded", False)) and str(summary.get("qqq_exec_symbol") or "").strip().upper() == symbol:
                qqq_expected_samples += 1

        audit_day_dir = audit_root / day_dir.name
        audit_files = sorted(audit_day_dir.glob("*.jsonl")) if audit_day_dir.exists() else []
        reasons = []
        if len(sample_paths) < int(min_total_samples):
            reasons.append("insufficient_total_samples")
        if blocked_samples < int(min_blocked_samples):
            reasons.append("insufficient_blocked_samples")
        if not audit_files:
            reasons.append("missing_audit_files")
        if qqq_expected_samples <= 0:
            reasons.append("no_same_symbol_qqq_samples")

        ready = len(reasons) == 0
        if ready:
            ready_days += 1

        gaps = {
            "total_samples_missing": max(0, int(min_total_samples) - int(len(sample_paths))),
            "blocked_samples_missing": max(0, int(min_blocked_samples) - int(blocked_samples)),
            "audit_files_missing": 0 if audit_files else 1,
            "same_symbol_qqq_samples_missing": 0 if qqq_expected_samples > 0 else 1,
        }
        next_actions: list[str] = []
        if "insufficient_total_samples" in reasons:
            next_actions.append(f"collect_more_samples:{gaps['total_samples_missing']}")
        if "insufficient_blocked_samples" in reasons:
            next_actions.append(f"collect_more_blocked_samples:{gaps['blocked_samples_missing']}")
        if "missing_audit_files" in reasons:
            next_actions.append("ensure_audit_writer_and_runtime_audit_files")
        if "no_same_symbol_qqq_samples" in reasons:
            next_actions.append("capture_same_symbol_qqq_traded_samples")

        rows.append(
            {
                "day": day_dir.name,
                "sample_dir": str(day_dir),
                "sample_count": len(sample_paths),
                "blocked_sample_count": blocked_samples,
                "qqq_expected_sample_count": qqq_expected_samples,
                "audit_file_count": len(audit_files),
                "ready": ready,
                "reasons": reasons,
                "gaps": gaps,
                "next_actions": next_actions,
            }
        )

    report = {
        "sample_root": str(sample_root),
        "audit_root": str(audit_root),
        "min_total_samples": int(min_total_samples),
        "min_blocked_samples": int(min_blocked_samples),
        "day_count": total_days,
        "ready_day_count": ready_days,
        "has_any_ready_day": ready_days > 0,
        "rows": rows,
        "global_next_actions": [],
    }
    global_actions: list[str] = []
    if total_days <= 0:
        global_actions.extend(
            [
                "capture_real_session_samples_for_today",
                "ensure_runtime_audit_writer_enabled",
                "run_postclose_bundle_to_generate_readiness_inputs",
            ]
        )
    elif ready_days <= 0:
        global_actions.extend(
            [
                "collect_missing_samples_or_blocked_cases_for_unready_days",
                "ensure_audit_files_exist_for_each_sample_day",
                "rerun_real_session_proof_pipeline_after_collection",
            ]
        )
    report["global_next_actions"] = global_actions
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build readiness report for real-session dual-strategy proof inputs")
    parser.add_argument("--sample-root", default=str(ROOT / "runtime" / "state" / "index_super_win_shadow_samples"))
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-path", default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "real_session_proof_readiness.json"))
    parser.add_argument("--min-total-samples", type=int, default=1)
    parser.add_argument("--min-blocked-samples", type=int, default=1)
    args = parser.parse_args()

    report = build_real_session_proof_readiness(
        sample_root=Path(args.sample_root),
        audit_root=Path(args.audit_root),
        output_path=Path(args.output_path),
        min_total_samples=int(args.min_total_samples),
        min_blocked_samples=int(args.min_blocked_samples),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("has_any_ready_day")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
