from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_stocks_kernel_release_pipeline import run_stocks_kernel_release_pipeline


def _config(strategy_id: str) -> dict:
    return {
        "strategy_id": strategy_id,
        "ttl_ms": 2000,
        "extras": {
            "dry_run": True,
            "startup_preflight_enabled": False,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    }


def test_run_stocks_kernel_release_pipeline_reports_go_when_runtime_and_matrix_are_green(tmp_path: Path) -> None:
    report = run_stocks_kernel_release_pipeline(
        config=_config("stocks_kernel_release_pipeline_go_001"),
        runtime_state={},
        scenarios=[],
        audit_root=tmp_path / "audit",
        audit_date="2026-04-10",
        output_dir=tmp_path / "state",
    )

    assert report["pipeline_green"] is True
    assert report["recommendation"] == "GO"
    assert Path(report["report_path"]).exists()
    assert Path(report["readiness_report_path"]).exists()
    assert Path(report["matrix_report_path"]).exists()
    assert Path(report["launch_gate_report_path"]).exists()


def test_run_stocks_kernel_release_pipeline_blocks_when_readiness_has_kill_switch(tmp_path: Path) -> None:
    report = run_stocks_kernel_release_pipeline(
        config=_config("stocks_kernel_release_pipeline_block_001"),
        runtime_state={"kill_switches": {"TQQQ": "broker_truth_mismatch"}},
        scenarios=[],
        audit_root=tmp_path / "audit",
        audit_date="2026-04-10",
        output_dir=tmp_path / "state",
    )

    assert report["pipeline_green"] is False
    assert report["recommendation"] == "BLOCK"


def test_run_stocks_kernel_release_pipeline_blocks_when_trader_parity_gate_fails(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.json"
    summary_path = tmp_path / "summary.json"
    identity_path.write_text(
        json.dumps(
            {
                "ok": True,
                "parity": {
                    "allow_match_ratio": 0.99,
                    "side_match_ratio": 0.99,
                    "reason_match_ratio": 0.7,
                    "exec_symbol_match_ratio": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "strategy_sections": [
                    {"strategy_id": "qqq_intraday_001", "fills_count": 4, "open_trigger_count": 0, "position_notional_abs_usd": 0.0},
                    {
                        "strategy_id": "stocks_kernel_release_pipeline_parity_001",
                        "fills_count": 4,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    config = _config("stocks_kernel_release_pipeline_parity_001")
    config["extras"]["trader_parity_gate"] = {
        "enabled": True,
        "identity_report_path": str(identity_path),
        "daily_summary_report_path": str(summary_path),
    }
    report = run_stocks_kernel_release_pipeline(
        config=config,
        runtime_state={},
        scenarios=[],
        audit_root=tmp_path / "audit",
        audit_date="2026-04-10",
        output_dir=tmp_path / "state",
    )

    assert report["pipeline_green"] is False
    assert report["recommendation"] == "BLOCK"
    assert report["trader_parity_gate_enabled"] is True
    gate_report = json.loads(Path(report["launch_gate_report_path"]).read_text(encoding="utf-8"))
    assert "trader_parity_gate_failed" in gate_report["blockers"]


def test_run_stocks_kernel_release_pipeline_reports_go_when_trader_parity_gate_passes(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.json"
    summary_path = tmp_path / "summary.json"
    identity_path.write_text(
        json.dumps(
            {
                "ok": True,
                "parity": {
                    "allow_match_ratio": 0.99,
                    "side_match_ratio": 0.99,
                    "reason_match_ratio": 0.99,
                    "exec_symbol_match_ratio": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(
            {
                "strategy_sections": [
                    {"strategy_id": "qqq_intraday_001", "fills_count": 4, "open_trigger_count": 0, "position_notional_abs_usd": 0.0},
                    {
                        "strategy_id": "stocks_kernel_release_pipeline_parity_go_001",
                        "fills_count": 4,
                        "open_trigger_count": 0,
                        "position_notional_abs_usd": 0.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    config = _config("stocks_kernel_release_pipeline_parity_go_001")
    config["extras"]["trader_parity_gate"] = {
        "enabled": True,
        "identity_report_path": str(identity_path),
        "daily_summary_report_path": str(summary_path),
    }
    report = run_stocks_kernel_release_pipeline(
        config=config,
        runtime_state={},
        scenarios=[],
        audit_root=tmp_path / "audit",
        audit_date="2026-04-10",
        output_dir=tmp_path / "state",
    )

    assert report["pipeline_green"] is True
    assert report["recommendation"] == "GO"
    assert report["trader_parity_gate_enabled"] is True
    assert Path(report["trader_parity_gate_report_path"]).exists()


def test_run_stocks_kernel_release_pipeline_blocks_when_trader_parity_gate_paths_missing(tmp_path: Path) -> None:
    config = _config("stocks_kernel_release_pipeline_parity_missing_paths_001")
    config["extras"]["trader_parity_gate"] = {
        "enabled": True,
        "identity_report_path": "",
        "daily_summary_report_path": "",
    }
    report = run_stocks_kernel_release_pipeline(
        config=config,
        runtime_state={},
        scenarios=[],
        audit_root=tmp_path / "audit",
        audit_date="2026-04-10",
        output_dir=tmp_path / "state",
    )

    assert report["pipeline_green"] is False
    assert report["recommendation"] == "BLOCK"
    assert report["trader_parity_gate_enabled"] is True
    gate_payload = json.loads(Path(report["trader_parity_gate_report_path"]).read_text(encoding="utf-8"))
    assert gate_payload["recommendation"] == "HOLD"
    assert "trader_parity_gate_missing_report_path" in gate_payload["failed_checks"]


def test_run_stocks_kernel_release_pipeline_blocks_when_parity_required_but_gate_disabled(tmp_path: Path) -> None:
    config = _config("stocks_kernel_release_pipeline_parity_required_001")
    config["extras"]["policy_params"] = {"rule_engine_parity": True}
    report = run_stocks_kernel_release_pipeline(
        config=config,
        runtime_state={},
        scenarios=[],
        audit_root=tmp_path / "audit",
        audit_date="2026-04-10",
        output_dir=tmp_path / "state",
    )

    assert report["pipeline_green"] is False
    assert report["recommendation"] == "BLOCK"
    assert report["trader_parity_gate_enabled"] is False
    assert report["trader_parity_gate_required"] is True
    gate_payload = json.loads(Path(report["trader_parity_gate_report_path"]).read_text(encoding="utf-8"))
    assert gate_payload["recommendation"] == "HOLD"
    assert "trader_parity_gate_required_but_disabled" in gate_payload["failed_checks"]
