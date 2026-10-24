from __future__ import annotations

import json
from pathlib import Path

import yaml

from tool.index_super_win_replica_preflight import build_preflight_report


def _write_manifest(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "runtime_scope": "index_super_win_replica",
                "order_namespace": "iswv2",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                        "volatility_symbol": "QQQ",
                        "enabled": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_preflight_passes_when_manifest_runtime_and_broker_are_consistent(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    runtime_path = tmp_path / "runtime.json"
    broker_path = tmp_path / "broker.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"
    replay_dir = tmp_path / "tool" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "qqq_index_replica_replay.py").write_text("ok", encoding="utf-8")
    (replay_dir / "export_audit_trace.py").write_text("ok", encoding="utf-8")

    _write_manifest(manifest_path)
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")
    runtime_path.write_text(
        json.dumps(
            {
                "QQQ": {
                    "is_traded": True,
                    "active_side": "LONG",
                    "active_exec_symbol": "TQQQ",
                    "expected_entry_qty": 8,
                    "entry_px": 50.0,
                    "stop_px": 48.5,
                    "extras": {
                        "live_qty": 8,
                        "protection_active": True,
                        "entry_time_et": "2026-03-23T10:00:00-04:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    broker_path.write_text(json.dumps({"TQQQ": 8.0, "SQQQ": 0.0}), encoding="utf-8")

    payload = build_preflight_report(
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        runtime_snapshot_path=runtime_path,
        broker_positions_path=broker_path,
        diag_snapshot_path=diag_path,
        audit_root=audit_root,
    )

    assert payload["ready_for_enablement"] is True
    assert not [item for item in payload["findings"] if item["level"] == "FAIL"]


def test_preflight_fails_when_live_runtime_has_no_active_protection(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    runtime_path = tmp_path / "runtime.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"
    replay_dir = tmp_path / "tool" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "qqq_index_replica_replay.py").write_text("ok", encoding="utf-8")
    (replay_dir / "export_audit_trace.py").write_text("ok", encoding="utf-8")

    _write_manifest(manifest_path)
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")
    runtime_path.write_text(
        json.dumps(
            {
                "QQQ": {
                    "is_traded": True,
                    "active_side": "LONG",
                    "active_exec_symbol": "TQQQ",
                    "expected_entry_qty": 8,
                    "entry_px": 50.0,
                    "stop_px": 48.5,
                    "extras": {
                        "live_qty": 8,
                        "protection_active": False,
                        "entry_time_et": "2026-03-23T10:00:00-04:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    payload = build_preflight_report(
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        runtime_snapshot_path=runtime_path,
        broker_positions_path=None,
        diag_snapshot_path=diag_path,
        audit_root=audit_root,
    )

    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "live runtime has stop_px but protection_active=false" in messages


def test_preflight_fails_when_runtime_and_broker_disagree(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    runtime_path = tmp_path / "runtime.json"
    broker_path = tmp_path / "broker.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"
    replay_dir = tmp_path / "tool" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "qqq_index_replica_replay.py").write_text("ok", encoding="utf-8")
    (replay_dir / "export_audit_trace.py").write_text("ok", encoding="utf-8")

    _write_manifest(manifest_path)
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")
    runtime_path.write_text(
        json.dumps(
            {
                "QQQ": {
                    "is_traded": False,
                    "active_side": None,
                    "active_exec_symbol": "TQQQ",
                    "expected_entry_qty": 0,
                    "extras": {
                        "live_qty": 0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    broker_path.write_text(json.dumps({"TQQQ": 5.0, "SQQQ": 0.0}), encoding="utf-8")

    payload = build_preflight_report(
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        runtime_snapshot_path=runtime_path,
        broker_positions_path=broker_path,
        diag_snapshot_path=diag_path,
        audit_root=audit_root,
    )

    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "runtime flat but broker snapshot non-zero" in messages


def test_preflight_fails_when_required_snapshots_are_missing(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"

    _write_manifest(manifest_path)
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")

    payload = build_preflight_report(
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        runtime_snapshot_path=None,
        broker_positions_path=None,
        diag_snapshot_path=diag_path,
        audit_root=audit_root,
    )

    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "runtime snapshot argument missing" in messages
    assert "broker positions snapshot argument missing" in messages


def test_preflight_fails_when_strategy_id_collides_with_qqq(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    runtime_path = tmp_path / "runtime.json"
    broker_path = tmp_path / "broker.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"
    replay_dir = tmp_path / "tool" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "qqq_index_replica_replay.py").write_text("ok", encoding="utf-8")
    (replay_dir / "export_audit_trace.py").write_text("ok", encoding="utf-8")

    _write_manifest(manifest_path)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["strategy_id"] = "qqq_intraday_001"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")
    runtime_path.write_text(json.dumps({"QQQ": {"is_traded": False, "extras": {"live_qty": 0}}}), encoding="utf-8")
    broker_path.write_text(json.dumps({"TQQQ": 0.0, "SQQQ": 0.0}), encoding="utf-8")

    payload = build_preflight_report(
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        runtime_snapshot_path=runtime_path,
        broker_positions_path=broker_path,
        diag_snapshot_path=diag_path,
        audit_root=audit_root,
    )
    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "strategy_id collides with qqq strategy_id" in messages


def test_preflight_fails_when_safety_switches_disabled(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    baseline_path = tmp_path / "baseline.json"
    runtime_path = tmp_path / "runtime.json"
    broker_path = tmp_path / "broker.json"
    diag_path = tmp_path / "runtime" / "state" / "diag.json"
    audit_root = tmp_path / "runtime" / "audit"
    replay_dir = tmp_path / "tool" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "qqq_index_replica_replay.py").write_text("ok", encoding="utf-8")
    (replay_dir / "export_audit_trace.py").write_text("ok", encoding="utf-8")

    _write_manifest(manifest_path)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["enable_local_audit_bridge"] = False
    manifest["kill_switch_on_unprotected_live"] = False
    manifest["stop_intent_dedupe_sec"] = 0
    manifest["zero_position_flat_confirmations"] = 0
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    baseline_path.write_text(json.dumps({"baseline": True}), encoding="utf-8")
    runtime_path.write_text(json.dumps({"QQQ": {"is_traded": False, "extras": {"live_qty": 0}}}), encoding="utf-8")
    broker_path.write_text(json.dumps({"TQQQ": 0.0, "SQQQ": 0.0}), encoding="utf-8")

    payload = build_preflight_report(
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        runtime_snapshot_path=runtime_path,
        broker_positions_path=broker_path,
        diag_snapshot_path=diag_path,
        audit_root=audit_root,
    )
    assert payload["ready_for_enablement"] is False
    messages = [item["message"] for item in payload["findings"] if item["level"] == "FAIL"]
    assert "local audit bridge must be enabled" in messages
    assert "kill_switch_on_unprotected_live must be enabled" in messages
    assert "stop_intent_dedupe_sec must be > 0" in messages
    assert "zero_position_flat_confirmations must be >= 1" in messages
