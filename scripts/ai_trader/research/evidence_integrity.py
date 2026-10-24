from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_evidence_integrity_report(
    *,
    cycle_id: str,
    evidence_paths: Mapping[str, str | Path],
) -> Dict[str, Any]:
    required_keys = [
        "admission_gate",
        "health_monitor",
        "operator_controls",
        "capital_governor",
        "session_control",
        "execution_feedback",
        "candidate_lifecycle",
    ]
    items: list[Dict[str, Any]] = []
    missing: list[str] = []
    for key in required_keys:
        raw_path = evidence_paths.get(key)
        path_text = str(raw_path or "")
        exists = bool(path_text) and Path(path_text).exists()
        status = "ok" if exists else "missing"
        if not exists:
            missing.append(key)
        items.append(
            {
                "evidence_key": key,
                "path": path_text,
                "exists": exists,
                "status": status,
            }
        )

    overall = "passed" if not missing else "blocked"
    blocking_issues = [f"missing_evidence:{name}" for name in missing]
    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "overall_status": overall,
        "checks": items,
        "missing_evidence": missing,
        "blocking_issues": blocking_issues,
        "next_actions": (
            ["collect_missing_evidence_and_rerun_preflight"]
            if missing
            else ["evidence_integrity_passed"]
        ),
    }

