from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


DEFAULT_FREEZE_BASELINE_PATH = "docs/research_safety_contract_freeze_baseline.json"
DEFAULT_GATE_REPORT_PATH = "runs/ai_trader/research/state/research_safety_contract_gate_report.json"


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


def _extract_three_entry_payload(doc: Mapping[str, Any]) -> Dict[str, Any]:
    observability = _as_dict(_as_dict(doc).get("policy_compatibility_observability"))
    migration_notice = _as_dict(observability.get("migration_notice"))
    notice_lifecycle = _as_dict(observability.get("notice_lifecycle"))
    return {
        "status": migration_notice.get("status"),
        "level": migration_notice.get("level"),
        "active": migration_notice.get("active"),
        "silenced": migration_notice.get("silenced"),
        "throttle_key": migration_notice.get("throttle_key"),
        "first_seen": notice_lifecycle.get("first_seen"),
        "last_seen": notice_lifecycle.get("last_seen"),
        "last_level": notice_lifecycle.get("last_level"),
        "last_throttle_key": notice_lifecycle.get("last_throttle_key"),
        "silenced_until": notice_lifecycle.get("silenced_until"),
    }


def _field_diff(*, expected: Iterable[str], observed: Iterable[str]) -> Dict[str, list[str]]:
    expected_set = {str(x) for x in expected}
    observed_set = {str(x) for x in observed}
    return {
        "missing": sorted(expected_set - observed_set),
        "extra": sorted(observed_set - expected_set),
    }


def evaluate_freeze_change_gate(
    *,
    baseline_path: str | Path = DEFAULT_FREEZE_BASELINE_PATH,
    report_path: str | Path = DEFAULT_GATE_REPORT_PATH,
    persist: bool = True,
) -> Dict[str, Any]:
    baseline = _load_json(baseline_path)
    changed_artifacts: list[Dict[str, Any]] = []
    changed_contract_fields: list[Dict[str, Any]] = []
    changed_aliases: list[Dict[str, Any]] = []
    reasons: list[str] = []

    if not baseline:
        report = {
            "baseline_path": str(baseline_path),
            "report_path": str(report_path),
            "changed_artifacts": [{"path": str(baseline_path), "change": "missing_or_invalid_baseline"}],
            "changed_contract_fields": [],
            "changed_aliases": [],
            "execution_guard_status": {"passed": False, "checks": []},
            "gate_status": "hold",
            "reasons": ["freeze_baseline_missing_or_invalid"],
        }
        if persist:
            out = Path(report_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    core_state_files = [str(x) for x in _as_list(baseline.get("core_state_files")) if str(x)]
    readonly_view_files = [str(x) for x in _as_list(baseline.get("readonly_view_files")) if str(x)]
    contract_fields = _as_dict(baseline.get("contract_fields"))

    for path_text in core_state_files:
        if not Path(path_text).exists():
            changed_artifacts.append({"path": path_text, "change": "missing"})
    for path_text in readonly_view_files:
        if not Path(path_text).exists():
            changed_artifacts.append({"path": path_text, "change": "missing"})

    view_docs: list[Dict[str, Any]] = []
    for path_text in readonly_view_files:
        payload = _load_json(path_text)
        if not payload:
            changed_artifacts.append({"path": path_text, "change": "invalid_json_or_empty"})
        else:
            view_docs.append(payload)

    if view_docs:
        for view_path, view in zip(readonly_view_files, view_docs):
            observability = _as_dict(view.get("policy_compatibility_observability"))
            contract_groups = {
                "policy_compatibility_observability": observability,
                "migration_notice": _as_dict(observability.get("migration_notice")),
                "notice_lifecycle": _as_dict(observability.get("notice_lifecycle")),
                "migration_progress": _as_dict(observability.get("migration_progress")),
            }
            for group_name, observed_obj in contract_groups.items():
                expected_fields = [str(x) for x in _as_list(contract_fields.get(group_name)) if str(x)]
                if not expected_fields:
                    continue
                diff = _field_diff(expected=expected_fields, observed=observed_obj.keys())
                if diff["missing"] or diff["extra"]:
                    changed_contract_fields.append(
                        {
                            "path": view_path,
                            "scope": group_name,
                            "missing": diff["missing"],
                            "extra": diff["extra"],
                        }
                    )

        lifecycle_idx = 2
        if len(core_state_files) > lifecycle_idx:
            lifecycle_state_path = core_state_files[lifecycle_idx]
            lifecycle_state = _load_json(lifecycle_state_path)
            expected_state_fields = [str(x) for x in _as_list(contract_fields.get("notice_lifecycle_state_file")) if str(x)]
            if expected_state_fields and lifecycle_state:
                diff = _field_diff(expected=expected_state_fields, observed=lifecycle_state.keys())
                if diff["missing"] or diff["extra"]:
                    changed_contract_fields.append(
                        {
                            "path": lifecycle_state_path,
                            "scope": "notice_lifecycle_state_file",
                            "missing": diff["missing"],
                            "extra": diff["extra"],
                        }
                    )

        consistency_keys = [str(x) for x in _as_list(contract_fields.get("three_entry_consistency")) if str(x)]
        if len(view_docs) >= 3 and consistency_keys:
            payloads = [_extract_three_entry_payload(view_docs[0]), _extract_three_entry_payload(view_docs[1]), _extract_three_entry_payload(view_docs[2])]
            for key_name in consistency_keys:
                values = [payload.get(key_name) for payload in payloads]
                if not (values[0] == values[1] == values[2]):
                    changed_contract_fields.append(
                        {
                            "path": "three_entry_consistency",
                            "scope": key_name,
                            "missing": [],
                            "extra": [str(v) for v in values],
                        }
                    )

        aliases = [_as_dict(x) for x in _as_list(baseline.get("compatibility_aliases"))]
        if aliases:
            primary_observability = _as_dict(view_docs[0].get("policy_compatibility_observability"))
            for alias in aliases:
                alias_name = str(alias.get("alias") or "")
                if alias_name == "recent_hits":
                    if primary_observability.get("recent_hits") != primary_observability.get("recent_examples"):
                        changed_aliases.append(
                            {
                                "alias": "recent_hits",
                                "canonical": "recent_examples",
                                "change": "value_mismatch",
                            }
                        )
                elif alias_name == "policy_compatibility":
                    if not any("policy_compatibility" in view for view in view_docs):
                        changed_aliases.append(
                            {
                                "alias": "policy_compatibility",
                                "canonical": "policy_compatibility_observability.current_cycle",
                                "change": "missing_in_all_readonly_views",
                            }
                        )

    guard_rows = [_as_dict(x) for x in _as_list(baseline.get("execution_chain_unchanged_guards"))]
    guard_checks: list[Dict[str, Any]] = []
    for row in guard_rows:
        file_path = str(row.get("file") or "")
        must_contain = str(row.get("must_contain") or "")
        text = Path(file_path).read_text(encoding="utf-8") if file_path and Path(file_path).exists() else ""
        matched = bool(must_contain and must_contain in text)
        guard_checks.append({"file": file_path, "matched": matched})
    execution_guard_passed = all(bool(item.get("matched")) for item in guard_checks) if guard_checks else False

    if changed_artifacts:
        reasons.append("frozen_artifact_changed_or_missing")
    if changed_contract_fields:
        reasons.append("frozen_contract_field_drift_detected")
    if changed_aliases:
        reasons.append("compatibility_alias_drift_detected")
    if not execution_guard_passed:
        reasons.append("execution_chain_guard_missing")

    gate_status = "pass"
    if reasons:
        gate_status = "hold"
    else:
        reasons = ["no_frozen_scope_change_detected_or_baseline_aligned"]

    report = {
        "baseline_path": str(baseline_path),
        "report_path": str(report_path),
        "changed_artifacts": changed_artifacts,
        "changed_contract_fields": changed_contract_fields,
        "changed_aliases": changed_aliases,
        "execution_guard_status": {
            "passed": bool(execution_guard_passed),
            "checks": guard_checks,
        },
        "gate_status": gate_status,
        "reasons": reasons,
    }
    if persist:
        out = Path(report_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> None:
    report = evaluate_freeze_change_gate()
    print("freeze_change_gate_report:", report["report_path"])
    print("gate_status:", report["gate_status"])


if __name__ == "__main__":
    main()
