from __future__ import annotations

import json
from pathlib import Path

import yaml

from tool.run_index_super_win_session_enablement_checks import run_session_enablement_checks


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_session_config(path: Path, *, strategy_id: str, runtime_scope: str, runtime_snapshot_path: str, session_name: str, start: str, end: str) -> None:
    _write_yaml(
        path,
        {
            "strategy_id": strategy_id,
            "runtime_scope": runtime_scope,
            "runtime_snapshot_path": runtime_snapshot_path,
            "order_namespace": "iswv2",
            "enable_local_audit_bridge": True,
            "kill_switch_on_unprotected_live": True,
            "zero_position_flat_confirmations": 1,
            "session_name": session_name,
            "session_start_et": start,
            "session_end_et": end,
            "replica_manifests": [
                {
                    "key": "qqq_pair",
                    "signal_symbol": "QQQ",
                    "confirm_symbol": "QQEW",
                    "volatility_symbol": "VIX",
                    "long_exec_symbol": "TQQQ",
                    "short_exec_symbol": "SQQQ",
                }
            ],
        },
    )


def test_run_session_enablement_checks_writes_bundle_session_and_final_reports(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    _write_yaml(
        config_path,
        {
            "strategies": [
                {
                    "id": "index_super_win_001",
                    "enabled": False,
                    "module": "strategies.index_super_win_replica_strategy",
                    "class": "IndexSuperWinReplicaStrategy",
                }
            ]
        },
    )
    pre = tmp_path / "pre.yml"
    after = tmp_path / "after.yml"
    _write_session_config(
        pre,
        strategy_id="qqq_premarket_001",
        runtime_scope="index_super_win_replica_premarket",
        runtime_snapshot_path="runtime/state/pre.json",
        session_name="premarket",
        start="04:00:00",
        end="09:29:59",
    )
    _write_session_config(
        after,
        strategy_id="qqq_afterhours_001",
        runtime_scope="index_super_win_replica_afterhours",
        runtime_snapshot_path="runtime/state/after.json",
        session_name="afterhours",
        start="16:00:00",
        end="19:59:59",
    )
    _write_json(tmp_path / "runtime" / "state" / "pre.json", {"QQQ": {"is_traded": False, "expected_entry_qty": 0, "extras": {"live_qty": 0}}})
    _write_json(tmp_path / "runtime" / "state" / "after.json", {"QQQ": {"is_traded": False, "expected_entry_qty": 0, "extras": {"live_qty": 0}}})
    broker_positions_path = tmp_path / "broker_positions.json"
    _write_json(broker_positions_path, {"TQQQ": 0.0, "SQQQ": 0.0})
    real_summary = tmp_path / "real_summary.json"
    batch = tmp_path / "batch.json"
    isolation = tmp_path / "isolation.json"
    trace_smoke = tmp_path / "trace_smoke.json"
    _write_json(real_summary, {"has_any_ready_day": True, "pipeline_status": "batch_completed", "ready_days": ["2026-03-26"], "needs_more_real_samples": False})
    _write_json(batch, {"green_days": 3, "enablement_attestation_green_days": 3, "rows": [{"day": "2026-03-26", "enablement_attestation_green": True}]})
    _write_json(isolation, {"high_value_event_count": 0})
    _write_json(trace_smoke, {"ready_for_trace_parity_capture": True, "observed": {"row_count": 1, "trace_count": 1, "decision_symbol_count": 1}})

    bundle_output = tmp_path / "bundle.json"
    per_instance_output_dir = tmp_path / "preflight"
    final_output = tmp_path / "final.json"
    report = run_session_enablement_checks(
        config_paths=[pre, after],
        broker_positions_path=broker_positions_path,
        bundle_output_path=bundle_output,
        per_instance_output_dir=per_instance_output_dir,
        final_checklist_output_path=final_output,
        config_path=config_path,
        strategy_id="index_super_win_001",
        expected_replica_module="strategies.index_super_win_replica_strategy",
        expected_replica_class="IndexSuperWinReplicaStrategy",
        real_session_summary_path=real_summary,
        batch_report_path=batch,
        isolation_daily_summary_path=isolation,
        trace_smoke_gate_path=trace_smoke,
        required_green_days=3,
        max_high_value_isolation_events=0,
    )

    assert report["bundle_ready_for_enablement"] is True
    assert len(report["session_reports"]) == 2
    assert bundle_output.exists()
    assert final_output.exists()
    assert (per_instance_output_dir / "qqq_premarket_001.json").exists()
    assert (per_instance_output_dir / "qqq_afterhours_001.json").exists()


def test_run_session_enablement_checks_fails_when_bundle_is_not_green(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    _write_yaml(
        config_path,
        {
            "strategies": [
                {
                    "id": "index_super_win_001",
                    "enabled": False,
                    "module": "strategies.index_super_win_replica_strategy",
                    "class": "IndexSuperWinReplicaStrategy",
                }
            ]
        },
    )
    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    _write_session_config(
        a,
        strategy_id="dup",
        runtime_scope="index_super_win_replica_a",
        runtime_snapshot_path="runtime/state/a.json",
        session_name="premarket",
        start="04:00:00",
        end="09:29:59",
    )
    _write_session_config(
        b,
        strategy_id="dup",
        runtime_scope="index_super_win_replica_b",
        runtime_snapshot_path="runtime/state/b.json",
        session_name="overnight",
        start="09:00:00",
        end="12:00:00",
    )
    _write_json(tmp_path / "runtime" / "state" / "a.json", {"QQQ": {"is_traded": False, "expected_entry_qty": 0, "extras": {"live_qty": 0}}})
    _write_json(tmp_path / "runtime" / "state" / "b.json", {"QQQ": {"is_traded": False, "expected_entry_qty": 0, "extras": {"live_qty": 0}}})
    broker_positions_path = tmp_path / "broker_positions.json"
    _write_json(broker_positions_path, {"TQQQ": 0.0, "SQQQ": 0.0})
    real_summary = tmp_path / "real_summary.json"
    batch = tmp_path / "batch.json"
    isolation = tmp_path / "isolation.json"
    trace_smoke = tmp_path / "trace_smoke.json"
    _write_json(real_summary, {"has_any_ready_day": True, "pipeline_status": "batch_completed", "ready_days": ["2026-03-26"], "needs_more_real_samples": False})
    _write_json(batch, {"green_days": 3, "enablement_attestation_green_days": 3, "rows": [{"day": "2026-03-26", "enablement_attestation_green": True}]})
    _write_json(isolation, {"high_value_event_count": 0})
    _write_json(trace_smoke, {"ready_for_trace_parity_capture": True, "observed": {"row_count": 1, "trace_count": 1, "decision_symbol_count": 1}})

    report = run_session_enablement_checks(
        config_paths=[a, b],
        broker_positions_path=broker_positions_path,
        bundle_output_path=tmp_path / "bundle.json",
        per_instance_output_dir=tmp_path / "preflight",
        final_checklist_output_path=tmp_path / "final.json",
        config_path=config_path,
        strategy_id="index_super_win_001",
        expected_replica_module="strategies.index_super_win_replica_strategy",
        expected_replica_class="IndexSuperWinReplicaStrategy",
        real_session_summary_path=real_summary,
        batch_report_path=batch,
        isolation_daily_summary_path=isolation,
        trace_smoke_gate_path=trace_smoke,
        required_green_days=3,
        max_high_value_isolation_events=0,
    )

    assert report["ready_for_enablement"] is False
    assert report["bundle_ready_for_enablement"] is False
