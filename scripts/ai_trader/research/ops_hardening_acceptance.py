from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, Mapping


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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def write_json_crash_safe(path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=str(target.parent), delete=False) as tmp:
        tmp.write(json.dumps(dict(payload), indent=2, ensure_ascii=False))
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)


def write_jsonl_crash_safe(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=str(target.parent), delete=False) as tmp:
        for row in rows:
            tmp.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)


def load_json_safe(path: str | Path) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def build_artifact_index(
    *,
    cycle_id: str,
    run_id: str,
    cycle_dir: str | Path,
    state_root: str | Path,
    required_artifacts: Mapping[str, str | Path],
) -> Dict[str, Any]:
    croot = Path(cycle_dir).resolve()
    sroot = Path(state_root).resolve()
    items: list[Dict[str, Any]] = []
    missing: list[str] = []
    escaped: list[str] = []
    for key, raw_path in required_artifacts.items():
        p = Path(str(raw_path))
        resolved = p.resolve() if p.exists() else p
        exists = p.exists()
        if exists:
            if not (_within(resolved, croot) or _within(resolved, sroot)):
                escaped.append(str(resolved))
        else:
            missing.append(str(p))
        checksum = ""
        size_bytes = 0
        if exists and p.is_file():
            blob = p.read_bytes()
            checksum = sha1(blob).hexdigest()[:16]
            size_bytes = len(blob)
        items.append(
            {
                "artifact_key": str(key),
                "path": str(p),
                "exists": bool(exists),
                "size_bytes": int(size_bytes),
                "sha1_16": checksum,
            }
        )
    overall_status = "ok"
    if missing:
        overall_status = "missing_artifacts"
    if escaped:
        overall_status = "path_pollution"
    payload = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "generated_at": _iso_now(),
        "overall_status": overall_status,
        "artifact_count": len(items),
        "missing_artifacts": missing,
        "path_pollution": escaped,
        "artifacts": items,
    }
    path = Path(cycle_dir) / "artifact_index.json"
    write_json_crash_safe(path, payload)
    return {"artifact_index_json": str(path), "artifact_index": payload}


def build_consistency_report(
    *,
    cycle_id: str,
    run_id: str,
    incident_index: Mapping[str, Any],
    recovery_report: Mapping[str, Any],
    shift_summary: Mapping[str, Any],
    reliability_metrics: Mapping[str, Any],
    ops_registry: Mapping[str, Any],
    artifact_index: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    checks: list[Dict[str, Any]] = []
    mismatches: list[str] = []
    ai = _as_dict(artifact_index)
    if str(ai.get("overall_status") or "") != "ok":
        mismatches.append(f"artifact_index:{ai.get('overall_status')}")

    rules = [
        ("incident_index.day_id", str(_as_dict(incident_index).get("day_id") or ""), cycle_id),
        ("recovery_report.day_id", str(_as_dict(recovery_report).get("day_id") or ""), cycle_id),
        ("shift_summary.day_id", str(_as_dict(shift_summary).get("day_id") or ""), cycle_id),
        ("reliability_metrics.day_id", str(_as_dict(reliability_metrics).get("day_id") or ""), cycle_id),
        ("ops_registry.last_cycle_id", str(_as_dict(ops_registry).get("last_cycle_id") or ""), cycle_id),
    ]
    for name, actual, expected in rules:
        ok = actual == expected
        checks.append({"name": name, "actual": actual, "expected": expected, "ok": ok})
        if not ok:
            mismatches.append(name)

    run_rules = [
        ("incident_index.run_id", str(_as_dict(incident_index).get("run_id") or ""), run_id),
        ("recovery_report.run_id", str(_as_dict(recovery_report).get("run_id") or ""), run_id),
        ("shift_summary.run_id", str(_as_dict(shift_summary).get("run_id") or ""), run_id),
        ("reliability_metrics.run_id", str(_as_dict(reliability_metrics).get("run_id") or ""), run_id),
        ("ops_registry.run_id", str(_as_dict(ops_registry).get("run_id") or ""), run_id),
    ]
    for name, actual, expected in run_rules:
        ok = actual == expected
        checks.append({"name": name, "actual": actual, "expected": expected, "ok": ok})
        if not ok:
            mismatches.append(name)

    payload = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "generated_at": _iso_now(),
        "overall_status": "pass" if not mismatches else "fail",
        "checks": checks,
        "mismatches": mismatches,
    }
    path = Path(output_dir) / "consistency_report.json"
    write_json_crash_safe(path, payload)
    return {"consistency_report_json": str(path), "consistency_report": payload}


def build_postflight_check(
    *,
    cycle_id: str,
    run_id: str,
    preflight_check: Mapping[str, Any],
    consistency_report: Mapping[str, Any],
    recovery_report: Mapping[str, Any],
    incident_index: Mapping[str, Any],
    reliability_metrics: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    status = "ready"

    if str(_as_dict(preflight_check).get("overall_status") or "") == "blocked":
        status = "blocked"
        issues.append("preflight_blocked")
    elif str(_as_dict(preflight_check).get("overall_status") or "") == "degraded":
        status = "degraded"
        warnings.append("preflight_degraded")

    if str(_as_dict(consistency_report).get("overall_status") or "") != "pass":
        status = "blocked"
        issues.append("consistency_failed")

    incident = _as_dict(incident_index)
    critical = int(incident.get("critical_count") or 0)
    recovery_actions = _as_list(_as_dict(recovery_report).get("actions"))
    has_flatten_pause = any(str(_as_dict(x).get("action") or "") == "flatten_and_pause" for x in recovery_actions)
    if critical > 0 and not has_flatten_pause:
        status = "blocked"
        issues.append("critical_incident_without_flatten_and_pause")

    rel = _as_dict(reliability_metrics)
    if str(rel.get("status") or "") == "degraded":
        if status == "ready":
            status = "degraded"
        warnings.append("reliability_degraded")

    payload = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "generated_at": _iso_now(),
        "overall_status": status,
        "blocking_issues": issues,
        "warnings": warnings,
        "next_actions": (
            ["fix_blockers_then_resume"] if issues else ["monitor_degraded_signals"] if warnings else ["ready_for_acceptance_decision"]
        ),
    }
    path = Path(output_dir) / "postflight_check.json"
    write_json_crash_safe(path, payload)
    return {"postflight_check_json": str(path), "postflight_check": payload}


def build_acceptance_decision(
    *,
    cycle_id: str,
    run_id: str,
    preflight_check: Mapping[str, Any],
    postflight_check: Mapping[str, Any],
    reliability_metrics: Mapping[str, Any],
    incident_index: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    preflight = _as_dict(preflight_check)
    postflight = _as_dict(postflight_check)
    reliability = _as_dict(reliability_metrics)
    incident = _as_dict(incident_index)
    reasons: list[str] = []
    decision = "hold"

    if str(preflight.get("overall_status") or "") == "blocked":
        reasons.append("preflight_blocked")
    if str(postflight.get("overall_status") or "") == "blocked":
        reasons.append("postflight_blocked")
    if int(incident.get("open_count") or 0) > 0:
        reasons.append("open_incidents_present")
    if str(reliability.get("status") or "") == "degraded":
        reasons.append("reliability_degraded")

    if not reasons:
        decision = "accept"
    elif reasons == ["reliability_degraded"]:
        decision = "accept_with_guardrails"

    payload = {
        "cycle_id": cycle_id,
        "run_id": run_id,
        "generated_at": _iso_now(),
        "decision": decision,
        "accepted": decision in {"accept", "accept_with_guardrails"},
        "reasons": reasons,
        "required_actions": (
            [] if decision == "accept" else ["keep_paper_only", "resolve_issues_before_release"]
        ),
    }
    path = Path(output_dir) / "acceptance_decision.json"
    write_json_crash_safe(path, payload)
    return {"acceptance_decision_json": str(path), "acceptance_decision": payload}
