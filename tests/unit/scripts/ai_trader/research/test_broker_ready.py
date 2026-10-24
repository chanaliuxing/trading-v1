from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.broker_profile import build_broker_profile_report
from scripts.ai_trader.research.broker_safety_gate import build_broker_safety_gate


def test_broker_profile_real_default_disabled(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "operator_controls.json").write_text(
        json.dumps(
            {
                "global_pause": False,
                "paper_only": True,
                "live_enabled": False,
                "blocked_candidates": [],
                "blocked_symbols": [],
            }
        ),
        encoding="utf-8",
    )
    bundle = build_broker_profile_report(
        cycle_id="c-default-disabled",
        requested_profile="real",
        output_dir=tmp_path,
        state_root=state_root,
    )
    payload = bundle["broker_profile_report"]
    assert payload["broker_profile"] == "paper"
    assert payload["safe_default"] is True
    assert payload["launch_switch"]["real_broker_enabled"] is False


def test_broker_safety_gate_rejects_when_not_ready(tmp_path: Path) -> None:
    gate = build_broker_safety_gate(
        cycle_id="c-gate-reject",
        broker_profile_report={
            "broker_profile": "real",
            "credential_valid": False,
        },
        operator_controls={"live_enabled": False},
        preflight_check={"overall_status": "blocked"},
        admission_gate={"summary": {"approved": 0}},
        health_monitor_report={"summary": {"critical": 1}},
        kill_switch_report={"kill_switch_active": True},
        output_dir=tmp_path,
    )
    payload = gate["broker_safety_gate"]
    assert payload["gate_status"] == "rejected"
    assert payload["executable_real"] is False
    assert "broker_credential_valid" in payload["reasons"]
    assert Path(gate["broker_safety_gate_json"]).exists()

