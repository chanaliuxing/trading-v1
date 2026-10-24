from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_PREFLIGHT_RELEASE_CHECK_PATH = "runs/ai_trader/research/state/preflight_release_check.json"
DEFAULT_FALLBACK_PREFLIGHT_CHECK_PATH = "runs/ai_trader/research/cycles/fixture-2026-04-05/preflight_check.json"
DEFAULT_FREEZE_BASELINE_PATH = "docs/research_safety_contract_freeze_baseline.json"
DEFAULT_FREEZE_GATE_REPORT_PATH = "runs/ai_trader/research/state/research_safety_contract_gate_report.json"
DEFAULT_SIGNOFF_SUMMARY_PATH = "runs/ai_trader/research/state/release_signoff_summary.json"


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _load_json(path: str | Path) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _is_preflight_pass(payload: Mapping[str, Any]) -> bool:
    data = _as_dict(payload)
    status = str(data.get("status") or "").strip().lower()
    if status in {"pass", "passed", "ok", "ready"}:
        return True
    overall_status = str(data.get("overall_status") or "").strip().lower()
    return overall_status in {"ready", "pass", "passed", "ok"}


def build_release_signoff_summary(
    *,
    preflight_release_check_path: str | Path = DEFAULT_PREFLIGHT_RELEASE_CHECK_PATH,
    fallback_preflight_check_path: str | Path = DEFAULT_FALLBACK_PREFLIGHT_CHECK_PATH,
    freeze_baseline_path: str | Path = DEFAULT_FREEZE_BASELINE_PATH,
    freeze_gate_report_path: str | Path = DEFAULT_FREEZE_GATE_REPORT_PATH,
    output_path: str | Path = DEFAULT_SIGNOFF_SUMMARY_PATH,
) -> Dict[str, Any]:
    baseline = _load_json(freeze_baseline_path)
    gate_report = _load_json(freeze_gate_report_path)
    preflight = _load_json(preflight_release_check_path)
    preflight_source_path = str(preflight_release_check_path)
    if not preflight:
        fallback = _load_json(fallback_preflight_check_path)
        if fallback:
            preflight = fallback
            preflight_source_path = str(fallback_preflight_check_path)

    preflight_status = "pass" if _is_preflight_pass(preflight) else "fail"
    freeze_gate_status = str(gate_report.get("gate_status") or "hold")
    execution_guard_status = _as_dict(gate_report.get("execution_guard_status"))
    execution_guard_passed = bool(execution_guard_status.get("passed", False))

    required_paths = [
        str(preflight_source_path),
        str(freeze_gate_report_path),
    ]
    required_paths.extend([str(x) for x in _as_list(_as_dict(baseline).get("core_state_files")) if str(x)])
    required_paths.extend([str(x) for x in _as_list(_as_dict(baseline).get("readonly_view_files")) if str(x)])

    missing_paths = [path_text for path_text in required_paths if not Path(path_text).exists()]
    required_artifacts_present = len(missing_paths) == 0

    blockers: list[str] = []
    recommended_actions: list[str] = []

    if preflight_status != "pass":
        blockers.append("preflight_not_passed")
        recommended_actions.append("rerun_preflight_release_check_and_fix_blockers")
    if freeze_gate_status != "pass":
        blockers.append("freeze_gate_not_passed")
        recommended_actions.append("review_gate_report_and_align_with_freeze_baseline_or_contract_tests")
    if not execution_guard_passed:
        blockers.append("execution_guard_not_passed")
        recommended_actions.append("restore_execution_chain_guard_markers_before_signoff")
    if not required_artifacts_present:
        blockers.append("required_artifacts_missing")
        recommended_actions.append("regenerate_missing_artifacts_via_fixture_preflight_gate_pipeline")

    signoff_ready = len(blockers) == 0

    summary = {
        "preflight_status": preflight_status,
        "freeze_gate_status": freeze_gate_status,
        "execution_guard_status": {
            "passed": execution_guard_passed,
            "checks": _as_list(execution_guard_status.get("checks")),
        },
        "required_artifacts_present": required_artifacts_present,
        "signoff_ready": signoff_ready,
        "blockers": blockers,
        "recommended_actions": recommended_actions,
        "artifact_presence": {
            "missing": missing_paths,
            "required_count": len(required_paths),
        },
        "sources": {
            "preflight_release_check": preflight_source_path,
            "freeze_gate_report": str(freeze_gate_report_path),
            "freeze_baseline": str(freeze_baseline_path),
        },
    }

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "release_signoff_summary_json": str(target),
        "release_signoff_summary": summary,
    }


def main() -> None:
    bundle = build_release_signoff_summary()
    payload = _as_dict(bundle.get("release_signoff_summary"))
    print("release_signoff_summary:", bundle.get("release_signoff_summary_json"))
    print("signoff_ready:", payload.get("signoff_ready"))


if __name__ == "__main__":
    main()

