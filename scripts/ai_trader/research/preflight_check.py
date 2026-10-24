from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


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


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _status_rank(status: str) -> int:
    if status == "blocked":
        return 2
    if status == "degraded":
        return 1
    return 0


def _merge_status(current: str, new_status: str) -> str:
    return new_status if _status_rank(new_status) > _status_rank(current) else current


def _pick_text(value: Any) -> str:
    text = str(value or "").strip()
    return text


def _extract_policy_identity(row: Mapping[str, Any] | None) -> Dict[str, str]:
    payload = _as_dict(row)
    candidates = [
        payload,
        _as_dict(payload.get("preflight_policy")),
        _as_dict(payload.get("policy")),
        _as_dict(payload.get("policy_summary")),
        _as_dict(_as_dict(payload.get("policy_compatibility")).get("current_policy")),
    ]
    for candidate in candidates:
        version = (
            _pick_text(candidate.get("version"))
            or _pick_text(candidate.get("policy_version"))
            or _pick_text(candidate.get("schema_version"))
        )
        digest = (
            _pick_text(candidate.get("digest"))
            or _pick_text(candidate.get("policy_digest"))
            or _pick_text(candidate.get("signature"))
        )
        policy_id = (
            _pick_text(candidate.get("policy_id"))
            or _pick_text(candidate.get("id"))
            or _pick_text(candidate.get("name"))
        )
        if version or digest or policy_id:
            return {"policy_id": policy_id, "version": version, "digest": digest}
    return {"policy_id": "", "version": "", "digest": ""}


def _normalize_history_rows(preflight_history: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None) -> list[Dict[str, Any]]:
    payload = preflight_history
    if isinstance(payload, Mapping):
        container = _as_dict(payload)
        for key in ("history", "items", "rows", "entries", "reports"):
            rows = container.get(key)
            if isinstance(rows, (list, tuple, set)):
                return [_as_dict(x) for x in rows]
        if container:
            return [container]
        return []
    return [_as_dict(x) for x in _as_list(payload)]


def _build_policy_compatibility_summary(
    *,
    preflight_policy: Mapping[str, Any] | None,
    preflight_history: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None,
) -> Dict[str, Any]:
    current = _extract_policy_identity(preflight_policy)
    history_rows = _normalize_history_rows(preflight_history)
    history_identities = [_extract_policy_identity(x) for x in history_rows]
    known_versions = sorted({x["version"] for x in history_identities if x.get("version")})
    known_digests = sorted({x["digest"] for x in history_identities if x.get("digest")})

    advisories: list[str] = []
    anomalies: list[str] = []
    legacy_hints: list[str] = []

    version = _pick_text(current.get("version"))
    digest = _pick_text(current.get("digest"))
    policy_id = _pick_text(current.get("policy_id"))
    if not version:
        legacy_hints.append("current_policy_missing_version")
    if not digest:
        legacy_hints.append("current_policy_missing_digest")

    legacy_missing_rows = 0
    for item in history_identities:
        has_version = bool(_pick_text(item.get("version")))
        has_digest = bool(_pick_text(item.get("digest")))
        if not has_version or not has_digest:
            legacy_missing_rows += 1
    if legacy_missing_rows > 0:
        legacy_hints.append(f"legacy_history_reports_missing_fields:{legacy_missing_rows}")

    same_version_digests = {
        _pick_text(item.get("digest"))
        for item in history_identities
        if _pick_text(item.get("version")) == version and _pick_text(item.get("digest"))
    }
    if version and digest and same_version_digests and digest not in same_version_digests:
        anomalies.append("digest_changed_without_version_bump")
        advisories.append(
            f"policy_version_{version}_digest_changed_from_history"
        )

    history_match_by_version = int(sum(1 for x in history_identities if _pick_text(x.get("version")) == version))
    history_match_by_digest = int(sum(1 for x in history_identities if _pick_text(x.get("digest")) == digest))
    has_history = bool(history_rows)

    if not version and not digest:
        policy_state = "unknown"
        advisories.append("current_policy_identity_missing")
    elif not has_history:
        policy_state = "current_only"
        advisories.append("no_policy_history_available")
    elif history_match_by_version > 0 or history_match_by_digest > 0:
        policy_state = "known"
    else:
        policy_state = "unknown"
        advisories.append("current_policy_not_found_in_history")

    return {
        "policy_state": policy_state,
        "current_policy": {
            "policy_id": policy_id,
            "version": version,
            "digest": digest,
        },
        "history_summary": {
            "history_count": len(history_rows),
            "known_versions": known_versions,
            "known_digests_count": len(known_digests),
            "match_by_version_count": history_match_by_version,
            "match_by_digest_count": history_match_by_digest,
        },
        "anomalies": anomalies,
        "legacy_hints": legacy_hints,
        "advisories": advisories,
    }


def build_preflight_check(
    *,
    cycle_id: str,
    operator_controls: Mapping[str, Any],
    capital_governor_report: Mapping[str, Any],
    session_control_report: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    health_monitor_report: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    candidate_lifecycle_index: Mapping[str, Any],
    live_feedback_index: Mapping[str, Any],
    required_artifacts: Sequence[str],
    evidence_integrity_report: Mapping[str, Any] | None = None,
    preflight_policy: Mapping[str, Any] | None = None,
    preflight_history: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    freshness_max_hours: float = 48.0,
) -> Dict[str, Any]:
    checks: list[Dict[str, Any]] = []
    blocking_issues: list[str] = []
    warnings: list[str] = []
    next_actions: list[str] = []
    overall = "ready"

    controls = _as_dict(operator_controls)
    control_status = "ready"
    control_reasons: list[str] = []
    if bool(controls.get("global_pause")):
        control_status = "blocked"
        control_reasons.append("global_pause_enabled")
    elif not bool(controls.get("live_enabled")):
        control_status = "degraded"
        control_reasons.append("live_disabled")
    checks.append({"name": "operator_controls", "status": control_status, "evidence_ref": "operator_controls.json", "reasons": control_reasons})

    cap_summary = _as_dict(_as_dict(capital_governor_report).get("summary"))
    cap_status = "ready"
    cap_reasons: list[str] = []
    if int(cap_summary.get("rejected") or 0) > 0 and int(cap_summary.get("approved") or 0) == 0:
        cap_status = "degraded"
        cap_reasons.append("capital_rejections_present")
    checks.append({"name": "capital_governor", "status": cap_status, "evidence_ref": "capital_governor_report.json", "reasons": cap_reasons})

    session_summary = _as_dict(_as_dict(session_control_report).get("summary"))
    session_status = "ready"
    session_reasons: list[str] = []
    blocked = int(session_summary.get("blocked") or 0)
    allowed = int(session_summary.get("allowed") or 0)
    if blocked > 0 and allowed == 0:
        session_status = "blocked"
        session_reasons.append("all_candidates_blocked_by_session")
    elif blocked > 0:
        session_status = "degraded"
        session_reasons.append("partial_session_blocks")
    checks.append({"name": "session_control", "status": session_status, "evidence_ref": "session_control_report.json", "reasons": session_reasons})

    admission_summary = _as_dict(_as_dict(admission_gate).get("summary"))
    admission_status = "ready"
    admission_reasons: list[str] = []
    if int(admission_summary.get("approved") or 0) == 0:
        admission_status = "blocked"
        admission_reasons.append("no_admission_approved_candidates")
    elif int(admission_summary.get("rejected") or 0) > 0:
        admission_status = "degraded"
        admission_reasons.append("some_candidates_rejected")
    checks.append({"name": "admission_gate", "status": admission_status, "evidence_ref": "admission_gate.json", "reasons": admission_reasons})

    health_summary = _as_dict(_as_dict(health_monitor_report).get("summary"))
    health_status = "ready"
    health_reasons: list[str] = []
    if int(health_summary.get("critical") or 0) > 0:
        health_status = "blocked"
        health_reasons.append("critical_health_alerts_present")
    elif int(health_summary.get("warning") or 0) > 0:
        health_status = "degraded"
        health_reasons.append("health_warnings_present")
    checks.append({"name": "health_monitor", "status": health_status, "evidence_ref": "health_monitor_report.json", "reasons": health_reasons})

    registry_summary = _as_dict(_as_dict(active_registry).get("summary"))
    registry_status = "ready"
    registry_reasons: list[str] = []
    if int(registry_summary.get("active") or 0) == 0:
        registry_status = "degraded"
        registry_reasons.append("no_active_candidates")
    checks.append({"name": "active_registry", "status": registry_status, "evidence_ref": "active_registry.json", "reasons": registry_reasons})

    lifecycle_rows = _as_list(_as_dict(candidate_lifecycle_index).get("candidates"))
    lifecycle_status = "ready" if lifecycle_rows else "degraded"
    lifecycle_reasons = [] if lifecycle_rows else ["candidate_lifecycle_empty"]
    checks.append({"name": "candidate_lifecycle", "status": lifecycle_status, "evidence_ref": "candidate_lifecycle_index.json", "reasons": lifecycle_reasons})

    live_feedback_ts = _parse_ts(_as_dict(live_feedback_index).get("updated_at"))
    freshness_status = "ready"
    freshness_reasons: list[str] = []
    if live_feedback_ts is None:
        freshness_status = "blocked"
        freshness_reasons.append("live_feedback_missing_timestamp")
    else:
        age_hours = (datetime.now(timezone.utc) - live_feedback_ts).total_seconds() / 3600.0
        if age_hours > freshness_max_hours * 3:
            freshness_status = "blocked"
            freshness_reasons.append("live_feedback_stale")
        elif age_hours > freshness_max_hours:
            freshness_status = "degraded"
            freshness_reasons.append("live_feedback_old")
    checks.append({"name": "live_feedback_freshness", "status": freshness_status, "evidence_ref": "live_feedback_index.json", "reasons": freshness_reasons})

    missing_artifacts = [str(p) for p in required_artifacts if not Path(str(p)).exists()]
    artifact_status = "ready" if not missing_artifacts else "blocked"
    checks.append(
        {
            "name": "required_artifacts",
            "status": artifact_status,
            "evidence_ref": "artifact_paths",
            "missing": missing_artifacts,
            "required_count": len(list(required_artifacts)),
        }
    )
    integrity = _as_dict(evidence_integrity_report)
    if integrity:
        integrity_status = "ready" if str(integrity.get("overall_status") or "") == "passed" else "blocked"
        checks.append(
            {
                "name": "evidence_integrity",
                "status": integrity_status,
                "evidence_ref": "evidence_integrity_report.json",
                "reasons": [str(x) for x in _as_list(integrity.get("blocking_issues"))],
            }
        )

    for check in checks:
        status = str(check.get("status") or "ready")
        overall = _merge_status(overall, status)
        if status == "blocked":
            reason = f"{check.get('name')}: {', '.join(_as_list(check.get('reasons')) or _as_list(check.get('missing')) or ['blocked'])}"
            blocking_issues.append(reason)
        elif status == "degraded":
            reason = f"{check.get('name')}: {', '.join(_as_list(check.get('reasons')) or ['degraded'])}"
            warnings.append(reason)

    if blocking_issues:
        next_actions.append("resolve_blocking_issues_before_launch")
    if warnings:
        next_actions.append("review_degraded_checks_and_apply_controls")
    if not blocking_issues and not warnings:
        next_actions.append("ready_for_launch_verdict")

    policy_compatibility = _build_policy_compatibility_summary(
        preflight_policy=preflight_policy,
        preflight_history=preflight_history,
    )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "overall_status": overall,
        "checks": checks,
        "blocking_issues": blocking_issues,
        "warnings": warnings,
        "next_actions": next_actions,
        "policy_compatibility": policy_compatibility,
    }
