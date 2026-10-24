from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tool.report.build_paper_shadow_soak_gate_report import build_soak_gate_report


NOW = datetime.fromisoformat("2026-05-12T10:00:00-04:00")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _fresh_payload(**extra):
    payload = {"generated_at": "2026-05-12T09:59:30-04:00", "go_no_go": "go"}
    payload.update(extra)
    return payload


def _base_artifacts(root: Path) -> None:
    readiness = root / "runtime" / "readiness"
    _write_json(readiness / "broker_truth_snapshot_latest.json", _fresh_payload(source="paper_broker", broker_truth_backed=True))
    _write_json(readiness / "paper_broker_truth_snapshot_latest.json", _fresh_payload(source="paper_broker", broker_truth_backed=True))
    _write_json(readiness / "position_safety_latest.json", _fresh_payload(blockers=[]))
    _write_json(readiness / "production_state_latest.json", _fresh_payload(summary={"failure_count": 0}, buckets={}))
    _write_json(readiness / "runtime_version_latest.json", _fresh_payload(freshness={"loaded_code_current": True}))
    _write_jsonl(
        readiness / "strategy_tradability_heartbeat_20260512.jsonl",
        [_fresh_payload(strategy_id="s1", alive=True, tradable_entry_ready=False, idle_reason="offsession")],
    )
    _write_json(
        root / "reports" / "strategy_surface" / "live_strategy_surface_report_20260512.json",
        _fresh_payload(strategy_surface_summary={"canary_only_count": 1}),
    )
    _write_json(
        root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json",
        _fresh_payload(sample_count=1, conversion_rates={"approved_to_fill": 0.0}),
    )
    _write_json(
        root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json",
        _fresh_payload(sample_count=1, classification={"primary_rejection_driver": "INSUFFICIENT_SAMPLE"}),
    )
    _write_jsonl(
        root / "runtime" / "audit" / "paper_intents.jsonl",
        [
            {
                "stage": "paper_intent",
                "generated_at": "2026-05-12T09:59:40-04:00",
                "account_id": "PAPER",
                "strategy_id": "s1",
                "strategy_instance_id": "s1-a",
                "symbol": "SPY",
                "intent_id": "intent-1",
                "order_role": "ENTRY",
                "mode": "PAPER",
            }
        ],
    )


def test_soak_gate_passes_with_fresh_artifacts_and_green_invariants(tmp_path: Path):
    _base_artifacts(tmp_path)

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "PASS"
    assert report["summary"]["artifact_freshness_pass"] is True
    assert report["summary"]["owner_identity_pass"] is True


def test_soak_gate_fails_on_live_broker_leak(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(tmp_path / "runtime" / "audit" / "leak.json", {"generated_at": "2026-05-12T09:59:40-04:00", "message": "Connected to IBKR"})

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "FAIL"
    assert "live_broker_leak_evidence" in report["blockers"]


def test_soak_gate_fails_on_real_order_submit_marker(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(tmp_path / "runtime" / "audit" / "order.json", {"generated_at": "2026-05-12T09:59:40-04:00", "real_order_submit": True})

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "FAIL"
    assert "real_order_cancel_modify_marker" in report["blockers"]


def test_soak_gate_insufficient_when_required_artifacts_missing(tmp_path: Path):
    _base_artifacts(tmp_path)
    (tmp_path / "runtime" / "readiness" / "position_safety_latest.json").unlink()

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert "missing_artifact:position_safety" in report["blockers"]


def test_soak_gate_fails_on_stale_position_safety(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(tmp_path / "runtime" / "readiness" / "position_safety_latest.json", {"generated_at": "2026-05-12T09:00:00-04:00"})

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "FAIL"
    assert "stale_or_invalid_artifact:position_safety" in report["blockers"]


def test_soak_gate_fails_on_missing_owner_identity(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_jsonl(
        tmp_path / "runtime" / "audit" / "paper_intents.jsonl",
        [{"stage": "paper_intent", "account_id": "PAPER", "strategy_id": "s1", "symbol": "SPY", "intent_id": "intent-1", "order_role": "ENTRY"}],
    )

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "FAIL"
    assert "missing_owner_identity" in report["blockers"]


def test_soak_gate_fails_on_symbol_only_attribution(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(tmp_path / "runtime" / "audit" / "symbol_attribution.json", {"generated_at": "2026-05-12T09:59:40-04:00", "attribution_path": "symbol"})

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "FAIL"
    assert "symbol_only_attribution" in report["blockers"]


def test_soak_gate_warns_on_unknown_owner_manual_review_only(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(
        tmp_path / "runtime" / "audit" / "unknown_owner.json",
        {"generated_at": "2026-05-12T09:59:40-04:00", "owner_status": "UNKNOWN_OWNER", "manual_review_required": True},
    )

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "PASS"
    assert report["oms_invariants"]["unknown_owner_count"] >= 1
    assert report["oms_invariants"]["manual_review_count"] >= 1


def test_soak_gate_fails_on_unknown_owner_auto_cancel(tmp_path: Path):
    _base_artifacts(tmp_path)
    _write_json(tmp_path / "runtime" / "audit" / "unknown_auto_cancel.json", {"owner_status": "UNKNOWN_OWNER", "auto_cancel": True})

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["verdict"] == "FAIL"
    assert "unknown_owner_auto_cancel" in report["blockers"]


def test_soak_gate_includes_approved_to_fill_and_vegas_diagnostics(tmp_path: Path):
    _base_artifacts(tmp_path)

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW)

    assert report["approved_to_fill"]["sample_count"] == 1
    assert report["vegas_measurement"]["sample_count"] == 1


def test_soak_gate_writes_latest_json_and_markdown(tmp_path: Path):
    _base_artifacts(tmp_path)

    report = build_soak_gate_report(repo_root=tmp_path, now=NOW, report_day="20260512")

    assert report["verdict"] == "PASS"
    assert (tmp_path / "reports" / "paper_shadow_soak_gate" / "paper_shadow_soak_gate_latest.json").exists()
    assert (tmp_path / "docs" / "reports" / "paper_shadow_soak_gate_20260512.md").exists()
