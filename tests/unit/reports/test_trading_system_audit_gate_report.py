from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tool.report.build_trading_system_audit_gate_report import (
    audit_runtime_artifacts,
    build_audit_gate_report,
    run_static_scan,
)


NOW = datetime.fromisoformat("2026-05-12T10:00:00-04:00")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _fresh(**extra):
    payload = {"generated_at": "2026-05-12T09:59:30-04:00", "summary": {"ok": True}}
    payload.update(extra)
    return payload


def _base_artifacts(root: Path) -> None:
    _write_json(root / "runtime" / "readiness" / "production_state" / "production_state_latest.json", _fresh(go_no_go="go"))
    _write_json(
        root / "runtime" / "readiness" / "broker_truth_snapshot" / "broker_truth_snapshot_latest.json",
        _fresh(source="paper_broker", broker_truth_backed=True),
    )
    _write_json(root / "runtime" / "readiness" / "position_safety" / "position_safety_latest.json", _fresh(lookup_failed=False))
    _write_json(root / "runtime" / "readiness" / "runtime_version" / "runtime_version_latest.json", _fresh(version="test"))
    _write_jsonl(root / "runtime" / "readiness" / "strategy_tradability" / "strategy_tradability_20260512.jsonl", [_fresh(idle_reason="offsession")])
    _write_json(root / "reports" / "strategy_surface" / "live_strategy_surface_report_20260512.json", _fresh(strategy_surface_summary={}))
    _write_json(root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json", _fresh(sample_count=20))
    _write_json(root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json", _fresh(sample_count=20))


def _passing_commands() -> list[dict]:
    return [
        {"name": "no_live_guard", "returncode": 0, "summary": "4 passed"},
        {"name": "exec_contract", "returncode": 0, "summary": "passed"},
        {"name": "services_contract", "returncode": 0, "summary": "passed"},
        {"name": "ib_reconnect_fast", "returncode": 0, "summary": "passed"},
        {"name": "git_diff_check", "returncode": 0, "summary": ""},
    ]


def _pre_canary_commands() -> list[dict]:
    return _passing_commands() + [
        {"name": "reconciliation_contract", "returncode": 0, "summary": "passed"},
        {"name": "reports_contract", "returncode": 0, "summary": "passed"},
        {"name": "ib_reconnect_full", "returncode": 0, "summary": "passed"},
        {"name": "bounded_shadow_runtime", "returncode": 0, "summary": "passed"},
        {"name": "bounded_paper_runtime", "returncode": 0, "summary": "passed"},
        {"name": "strategy_execution_reliability_report", "returncode": 0, "summary": "passed"},
        {"name": "approved_to_fill_conversion_report", "returncode": 0, "summary": "passed"},
        {"name": "vegas_dlb_edge_measurement_report", "returncode": 0, "summary": "passed"},
        {"name": "paper_shadow_soak_gate_report", "returncode": 0, "summary": "passed"},
    ]


def test_static_scan_fails_unallowlisted_direct_order_mutation(tmp_path: Path):
    path = tmp_path / "strategies" / "bad_strategy.py"
    path.parent.mkdir(parents=True)
    path.write_text("def f(ib, contract, order):\n    return ib.placeOrder(contract, order)\n", encoding="utf-8")

    report = run_static_scan(repo_root=tmp_path)

    assert report["verdict"] == "FAIL"
    assert report["findings"][0]["risk"] == "broker_order_mutation"


def test_static_scan_allows_known_broker_adapter_surface(tmp_path: Path):
    path = tmp_path / "services" / "exec" / "adapters" / "ib_adapter.py"
    path.parent.mkdir(parents=True)
    path.write_text("from ib_insync import IB\nib = IB()\nib.placeOrder(contract, order)\n", encoding="utf-8")

    report = run_static_scan(repo_root=tmp_path)

    assert report["verdict"] == "PASS"
    assert report["allowlisted_finding_count"] == 2


def test_runtime_artifact_audit_passes_with_fresh_required_artifacts(tmp_path: Path):
    _base_artifacts(tmp_path)

    report = audit_runtime_artifacts(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "PASS"
    assert report["artifacts"]["production_state"]["fresh"] is True


def test_runtime_artifact_audit_insufficient_when_required_artifact_missing(tmp_path: Path):
    _base_artifacts(tmp_path)
    (tmp_path / "runtime" / "readiness" / "position_safety" / "position_safety_latest.json").unlink()

    report = audit_runtime_artifacts(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert "missing_artifact:position_safety" in report["blockers"]


def test_quick_audit_gate_writes_latest_json_and_markdown(tmp_path: Path):
    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="quick",
        command_results=_passing_commands(),
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "PASS"
    assert (tmp_path / "reports" / "audit_gate" / "audit_gate_latest.json").exists()
    assert (tmp_path / "docs" / "reports" / "audit_gate_20260512.md").exists()


def test_quick_audit_gate_requires_all_quick_commands(tmp_path: Path):
    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="quick",
        command_results=[{"name": "no_live_guard", "returncode": 0, "summary": "4 passed"}],
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert "missing_required_command:exec_contract" in report["blockers"]
    assert report["test_verdict"] == "INSUFFICIENT_DATA"


def test_full_audit_gate_uses_runtime_artifacts(tmp_path: Path):
    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="full",
        command_results=_passing_commands(),
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert "missing_artifact:production_state" in report["blockers"]


def test_pre_canary_requires_green_paper_shadow_diagnostics(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(
        tmp_path / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json",
        _fresh(sample_count=20, classification={"primary_bottleneck": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        _fresh(sample_count=20, classification={"primary_conversion_loss": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
        _fresh(sample_count=20, classification={"primary_rejection_driver": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json",
        _fresh(
            verdict="PASS",
            blockers=[],
            summary={"artifact_freshness_pass": True, "oms_invariants_pass": True, "no_live_leak": True, "owner_identity_pass": True},
            oms_invariants={"symbol_only_attribution_count": 0, "manual_review_count": 1, "unknown_owner_count": 1, "paper_shadow_intent_count": 20},
        ),
    )

    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="pre_canary",
        command_results=_pre_canary_commands(),
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "PASS"
    assert report["paper_shadow"]["verdict"] == "PASS"
    assert report["approved_scope"] == ["manual_canary_review"]
    assert report["oms_invariants"]["owner_identity"] is True


def test_pre_canary_rejects_low_sample_diagnostic(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(
        tmp_path / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json",
        _fresh(sample_count=1, classification={"primary_bottleneck": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        _fresh(sample_count=20, classification={"primary_conversion_loss": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
        _fresh(sample_count=20, classification={"primary_rejection_driver": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json",
        _fresh(verdict="PASS", blockers=[], summary={"artifact_freshness_pass": True, "oms_invariants_pass": True, "no_live_leak": True}),
    )

    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="pre_canary",
        command_results=_pre_canary_commands(),
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert "diagnostic_insufficient_data:strategy_execution_reliability" in report["blockers"]


def test_pre_canary_rejects_stale_diagnostic(tmp_path: Path):
    _base_artifacts(tmp_path)
    stale = {"generated_at": "2026-05-10T09:59:30-04:00", "sample_count": 20, "classification": {"primary_bottleneck": "NO_BOTTLENECK"}}
    _write_json(tmp_path / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json", stale)
    _write_json(
        tmp_path / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        _fresh(sample_count=20, classification={"primary_conversion_loss": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
        _fresh(sample_count=20, classification={"primary_rejection_driver": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json",
        _fresh(verdict="PASS", blockers=[], summary={"artifact_freshness_pass": True, "oms_invariants_pass": True, "no_live_leak": True}),
    )

    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="pre_canary",
        command_results=_pre_canary_commands(),
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert report["paper_shadow"]["diagnostics"]["strategy_execution_reliability"]["reason"] == "stale_or_missing_generated_at"


def test_pre_canary_rejects_soak_gate_pass_with_blockers(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(
        tmp_path / "reports" / "strategy_execution_reliability" / "strategy_execution_reliability_latest.json",
        _fresh(sample_count=20, classification={"primary_bottleneck": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        _fresh(sample_count=20, classification={"primary_conversion_loss": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
        _fresh(sample_count=20, classification={"primary_rejection_driver": "NO_BOTTLENECK"}),
    )
    _write_json(
        tmp_path / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json",
        _fresh(
            verdict="PASS",
            blockers=["live_broker_leak_evidence"],
            summary={"artifact_freshness_pass": True, "oms_invariants_pass": True, "no_live_leak": True},
        ),
    )

    report = build_audit_gate_report(
        repo_root=tmp_path,
        mode="pre_canary",
        command_results=_pre_canary_commands(),
        now=NOW,
        day="20260512",
    )

    assert report["verdict"] == "FAIL"
    assert "diagnostic_failed:paper_shadow_soak_gate" in report["blockers"]
