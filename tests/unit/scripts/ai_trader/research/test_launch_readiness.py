from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts.ai_trader.research.preflight_check import build_preflight_check
from scripts.ai_trader.research.readiness_dashboard import build_readiness_dashboard
from scripts.ai_trader.research.launch_checklist import build_launch_checklist
from scripts.ai_trader.research.launch_verdict import build_launch_verdict
from scripts.ai_trader.research.evidence_integrity import build_evidence_integrity_report
from scripts.ai_trader.research.launch_batch import build_launch_batch


def _base_inputs(now_ts: str) -> dict:
    return {
        "capital_governor_report": {"summary": {"approved": 1, "rejected": 0}},
        "session_control_report": {"summary": {"allowed": 1, "blocked": 0, "flatten": 0}},
        "admission_gate": {
            "summary": {"approved": 1, "rejected": 0},
            "items": [
                {
                    "candidate_id": "cand-1",
                    "experiment_id": "exp-1",
                    "mode": "paper",
                    "admission_status": "approved",
                }
            ],
        },
        "health_monitor_report": {"summary": {"warning": 0, "critical": 0}},
        "active_registry": {"summary": {"active": 1, "inactive": 0, "halted": 0}, "items": [{"candidate_id": "cand-1"}]},
        "candidate_lifecycle_index": {"candidates": [{"candidate_id": "cand-1"}]},
        "live_feedback_index": {"updated_at": now_ts, "items": []},
    }


def test_readiness_go_paper(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-go-paper",
        operator_controls={"global_pause": False, "live_enabled": False},
        required_artifacts=[str(existing)],
        **payload,
    )
    dashboard = build_readiness_dashboard(
        cycle_id="c-go-paper",
        preflight_check=preflight,
        admission_gate=payload["admission_gate"],
        health_monitor_report=payload["health_monitor_report"],
        active_registry=payload["active_registry"],
    )
    checklist = build_launch_checklist(
        cycle_id="c-go-paper",
        preflight_check=preflight,
        readiness_dashboard=dashboard,
    )
    verdict = build_launch_verdict(
        cycle_id="c-go-paper",
        preflight_check=preflight,
        launch_checklist=checklist,
        readiness_dashboard=dashboard,
        admission_gate=payload["admission_gate"],
    )
    assert dashboard["paper_ready"] is True
    assert verdict["verdict"] == "go_paper"
    assert "policy_compatibility" in dashboard
    assert "policy_compatibility" in verdict
    assert verdict["policy_compatibility"] == dashboard["policy_compatibility"]


def test_readiness_hold_live_when_live_controls_blocked(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-hold-live",
        operator_controls={"global_pause": True, "live_enabled": True},
        required_artifacts=[str(existing)],
        **payload,
    )
    dashboard = build_readiness_dashboard(
        cycle_id="c-hold-live",
        preflight_check=preflight,
        admission_gate=payload["admission_gate"],
        health_monitor_report=payload["health_monitor_report"],
        active_registry=payload["active_registry"],
    )
    checklist = build_launch_checklist(
        cycle_id="c-hold-live",
        preflight_check=preflight,
        readiness_dashboard=dashboard,
    )
    verdict = build_launch_verdict(
        cycle_id="c-hold-live",
        preflight_check=preflight,
        launch_checklist=checklist,
        readiness_dashboard=dashboard,
        admission_gate=payload["admission_gate"],
    )
    assert verdict["verdict"] == "hold_live"


def test_preflight_blocks_when_evidence_missing() -> None:
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-missing-evidence",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=["/tmp/definitely_missing_evidence_123456.json"],
        **payload,
    )
    assert preflight["overall_status"] == "blocked"
    required = next((c for c in preflight["checks"] if c["name"] == "required_artifacts"), {})
    assert required.get("status") == "blocked"
    assert required.get("missing")


def test_evidence_integrity_pass_and_block(tmp_path: Path) -> None:
    ok = tmp_path / "ok.json"
    ok.write_text("{}", encoding="utf-8")
    passed = build_evidence_integrity_report(
        cycle_id="c-pass",
        evidence_paths={
            "admission_gate": str(ok),
            "health_monitor": str(ok),
            "operator_controls": str(ok),
            "capital_governor": str(ok),
            "session_control": str(ok),
            "execution_feedback": str(ok),
            "candidate_lifecycle": str(ok),
        },
    )
    blocked = build_evidence_integrity_report(
        cycle_id="c-block",
        evidence_paths={
            "admission_gate": str(ok),
            "health_monitor": str(ok),
            "operator_controls": "/tmp/missing_1.json",
            "capital_governor": str(ok),
            "session_control": str(ok),
            "execution_feedback": str(ok),
            "candidate_lifecycle": str(ok),
        },
    )
    assert passed["overall_status"] == "passed"
    assert blocked["overall_status"] == "blocked"
    assert "operator_controls" in blocked["missing_evidence"]


def test_launch_batch_ready_and_partial_reject() -> None:
    launch_verdict = {"verdict": "go_paper"}
    admission_gate = {
        "items": [
            {"candidate_id": "c1", "experiment_id": "e1", "admission_status": "approved"},
            {"candidate_id": "c2", "experiment_id": "e2", "admission_status": "approved"},
            {"candidate_id": "c3", "experiment_id": "e3", "admission_status": "approved"},
        ]
    }
    allocation_plan = {
        "items": [
            {"experiment_id": "e1", "max_capital": 30000, "risk_budget_bps": 40, "target_weight": 0.3},
            {"experiment_id": "e2", "max_capital": 30000, "risk_budget_bps": 40, "target_weight": 0.3},
            {"experiment_id": "e3", "max_capital": 30000, "risk_budget_bps": 60, "target_weight": 0.4},
        ]
    }
    portfolio_selection = {
        "rejected_candidates": [
            {"candidate_id": "c3", "reason": "correlation_too_high"},
        ]
    }
    evidence = {"overall_status": "passed"}
    batch = build_launch_batch(
        cycle_id="c-batch",
        mode="paper",
        launch_verdict=launch_verdict,
        admission_gate=admission_gate,
        allocation_plan=allocation_plan,
        portfolio_selection=portfolio_selection,
        evidence_integrity_report=evidence,
        min_candidates=2,
    )
    assert batch["status"] == "ready"
    assert len(batch["candidates"]) >= 2
    assert any("correlation_too_high" in r.get("reasons", []) for r in batch["rejected_candidates"])


def test_preflight_policy_compatibility_flags_digest_change_without_version_bump(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-digest",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v1", "digest": "digest-new"},
        preflight_history=[
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-old"}},
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v0", "digest": "digest-v0"}},
        ],
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "known"
    assert "digest_changed_without_version_bump" in list(compat.get("anomalies") or [])


def test_preflight_policy_compatibility_unknown_and_legacy_hints(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-legacy",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v2", "digest": "digest-v2"},
        preflight_history=[
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1"}},
            {"some_legacy_row": True},
        ],
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    hints = list(compat.get("legacy_hints") or [])
    advisories = list(compat.get("advisories") or [])
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "unknown"
    assert any(str(x).startswith("legacy_history_reports_missing_fields:") for x in hints)
    assert "current_policy_not_found_in_history" in advisories


def test_preflight_policy_compatibility_current_only_without_history(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-current-only",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"},
        preflight_history=[],
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    advisories = list(compat.get("advisories") or [])
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "current_only"
    assert "no_policy_history_available" in advisories


def test_preflight_policy_compatibility_all_legacy_history_rows(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-all-legacy",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"},
        preflight_history=[
            {"preflight_policy": {"policy_id": "preflight-policy"}},
            {"policy_summary": {"id": "legacy-row"}},
            {"some_legacy_row": True},
        ],
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    hints = list(compat.get("legacy_hints") or [])
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "unknown"
    assert any(str(x).startswith("legacy_history_reports_missing_fields:") for x in hints)


def test_preflight_policy_compatibility_unknown_policy_vs_history(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-unknown",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v9", "digest": "digest-v9"},
        preflight_history=[
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v1"}},
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v2", "digest": "digest-v2"}},
        ],
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    advisories = list(compat.get("advisories") or [])
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "unknown"
    assert "current_policy_not_found_in_history" in advisories


def test_preflight_policy_compatibility_multi_version_migration_known(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-migration",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"},
        preflight_history=[
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v1"}},
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v2", "digest": "digest-v2"}},
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"}},
        ],
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    history = dict(compat.get("history_summary") or {})
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "known"
    assert history.get("match_by_version_count") == 1
    assert history.get("match_by_digest_count") == 1
    assert list(history.get("known_versions") or []) == ["v1", "v2", "v3"]


def test_preflight_policy_compatibility_accepts_history_container_payload(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-container-history",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"preflight_policy": {"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"}},
        preflight_history={
            "history": [
                {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v1"}},
                {"preflight_policy": {"policy_id": "preflight-policy", "version": "v2", "digest": "digest-v2"}},
                {"preflight_policy": {"policy_id": "preflight-policy", "version": "v3", "digest": "digest-v3"}},
            ]
        },
        **payload,
    )
    compat = preflight.get("policy_compatibility", {})
    history = dict(compat.get("history_summary") or {})
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "known"
    assert history.get("history_count") == 3
    assert history.get("match_by_version_count") == 1


def test_preflight_policy_compatibility_schema_locked(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-schema",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        preflight_policy={"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v2"},
        preflight_history=[
            {"preflight_policy": {"policy_id": "preflight-policy", "version": "v1", "digest": "digest-v1"}},
        ],
        **payload,
    )
    compat = dict(preflight.get("policy_compatibility") or {})
    assert set(compat.keys()) == {
        "policy_state",
        "current_policy",
        "history_summary",
        "anomalies",
        "legacy_hints",
        "advisories",
    }
    assert compat.get("policy_state") in {"known", "unknown", "current_only"}
    current = dict(compat.get("current_policy") or {})
    assert set(current.keys()) == {"policy_id", "version", "digest"}
    history = dict(compat.get("history_summary") or {})
    assert set(history.keys()) == {
        "history_count",
        "known_versions",
        "known_digests_count",
        "match_by_version_count",
        "match_by_digest_count",
    }
    for anomaly in list(compat.get("anomalies") or []):
        assert anomaly in {"digest_changed_without_version_bump"}


def test_preflight_policy_compatibility_legacy_call_without_policy_inputs(tmp_path: Path) -> None:
    existing = tmp_path / "ok.json"
    existing.write_text("{}", encoding="utf-8")
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = _base_inputs(now_ts)
    preflight = build_preflight_check(
        cycle_id="c-policy-compat-legacy-call",
        operator_controls={"global_pause": False, "live_enabled": True},
        required_artifacts=[str(existing)],
        **payload,
    )
    compat = dict(preflight.get("policy_compatibility") or {})
    current = dict(compat.get("current_policy") or {})
    assert preflight["overall_status"] == "ready"
    assert compat.get("policy_state") == "unknown"
    assert current == {"policy_id": "", "version": "", "digest": ""}
    assert "current_policy_identity_missing" in list(compat.get("advisories") or [])
