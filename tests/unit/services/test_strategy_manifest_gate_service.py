from __future__ import annotations

from datetime import datetime

from services.production.strategy_manifest_gate_service import build_strategy_manifest_gate


def _strategy(extras):
    return {
        "id": "paper_live_001",
        "enabled": True,
        "params": {
            "extras": {
                "data_contract": {"symbols": ["QQQ"]},
                **extras,
            }
        },
    }


def _valid_manifest():
    return {
        "production_blocking": True,
        "entry_contract": {"uses_decision_pipeline": True, "uom_only": True},
        "protection_contract": {"required": True, "fallback": "flatten"},
        "recovery_contract": {
            "policy_id": "stocks_kernel_close_only",
            "unknown_owner_action": "quarantine",
            "fallback": "flatten",
        },
        "eod_contract": {"no_overnight": True, "platform_eod_required": True},
        "tradability_reporting": {"reports_strategy_tradability_gate": True},
    }


def test_strategy_manifest_gate_accepts_legacy_session_policy(tmp_path):
    status = build_strategy_manifest_gate(
        config={
            "strategies": [
                _strategy(
                    {
                        "production_manifest": _valid_manifest(),
                        "force_flatten_time": "15:59:00",
                        "no_new_entry_after": "15:50:00",
                        "ab_time_start": "09:30:00",
                        "ab_time_end": "15:59:00",
                    }
                )
            ]
        },
        generated_at=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
        repo_root=tmp_path,
    )

    assert status["go_no_go"] == "go"
    assert status["session_policy_records"][0]["session_policy_source"] == "legacy_extras"
    assert (tmp_path / "runtime/readiness/strategy_manifest_gate/strategy_manifest_gate_latest.json").exists()


def test_strategy_manifest_gate_accepts_stock_kernel_session_aliases():
    status = build_strategy_manifest_gate(
        config={
            "strategies": [
                _strategy(
                    {
                        "production_manifest": _valid_manifest(),
                        "flatten_before_close_et": "15:59",
                        "no_new_after_et": "15:45",
                    }
                )
            ]
        },
        generated_at=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
    )

    assert status["go_no_go"] == "go"
    assert status["session_policy_records"][0]["force_flatten_deadline"] == "15:59"
    assert status["session_policy_records"][0]["entry_cutoff"] == "15:45"


def test_strategy_manifest_gate_accepts_option_policy_aliases():
    status = build_strategy_manifest_gate(
        config={
            "strategies": [
                _strategy(
                    {
                        "underlying": "QQQ",
                        "risk_symbol": "OPT:QQQ:0DTE",
                        "definition_id": "QQQ_INTRADAY_OPTION_PARITY_V1",
                        "contracts": 2,
                        "spread_max_pct": 0.15,
                        "dte_preferred": 0,
                        "production_manifest": {
                            **_valid_manifest(),
                            "recovery_contract": {
                                "policy_id": "option_close_only",
                                "unknown_owner_action": "quarantine",
                                "fallback": "flatten",
                            },
                            "option_contract": {"required": True},
                        },
                        "force_flatten_time": "15:58:00",
                        "no_new_entry_after": "15:00:00",
                    }
                )
            ]
        },
        generated_at=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
    )

    assert status["go_no_go"] == "go"
    assert status["option_policy_records"][0]["risk_symbol"] == "OPT:QQQ:0DTE"


def test_strategy_manifest_gate_blocks_option_missing_trigger_lineage():
    status = build_strategy_manifest_gate(
        config={
            "strategies": [
                _strategy(
                    {
                        "underlying": "QQQ",
                        "risk_symbol": "OPT:QQQ:0DTE",
                        "contracts": 2,
                        "spread_max_pct": 0.15,
                        "dte_preferred": 0,
                        "production_manifest": {
                            **_valid_manifest(),
                            "recovery_contract": {
                                "policy_id": "option_close_only",
                                "unknown_owner_action": "quarantine",
                                "fallback": "flatten",
                            },
                            "option_contract": {"required": True},
                        },
                        "force_flatten_time": "15:58:00",
                        "no_new_entry_after": "15:00:00",
                    }
                )
            ]
        },
        generated_at=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
    )

    assert status["go_no_go"] == "no_go"
    assert "paper_live_001:option_trigger_lineage_missing" in status["blockers"]


def test_strategy_manifest_gate_blocks_missing_force_flatten():
    status = build_strategy_manifest_gate(
        config={
            "strategies": [
                _strategy(
                    {
                        "production_manifest": _valid_manifest(),
                        "no_new_entry_after": "15:50:00",
                    }
                )
            ]
        },
        generated_at=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
    )

    assert status["go_no_go"] == "no_go"
    assert "paper_live_001:force_flatten_deadline_missing_or_invalid" in status["blockers"]


def test_strategy_manifest_gate_blocks_entry_cutoff_after_flatten():
    status = build_strategy_manifest_gate(
        config={
            "strategies": [
                _strategy(
                    {
                        "production_manifest": _valid_manifest(),
                        "force_flatten_time": "15:50:00",
                        "no_new_entry_after": "15:55:00",
                    }
                )
            ]
        },
        generated_at=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
    )

    assert status["go_no_go"] == "no_go"
    assert "paper_live_001:entry_cutoff_after_force_flatten" in status["blockers"]
