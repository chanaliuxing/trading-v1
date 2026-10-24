from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.active_registry import (
    load_active_registry,
    persist_active_registry,
    update_active_registry,
)
from scripts.ai_trader.research.admission_gate import build_admission_gate
from scripts.ai_trader.research.auto_halt import build_auto_halt_report
from scripts.ai_trader.research.health_monitor import build_health_monitor_report


def test_admission_health_halt_and_registry_flow(tmp_path: Path) -> None:
    lifecycle_index = {
        "candidates": [
            {"candidate_id": "cand-good", "experiment_id": "exp-good", "current_state": "paper_active"},
            {"candidate_id": "cand-bad", "experiment_id": "exp-bad", "current_state": "paper_active"},
        ]
    }
    retention = {
        "items": [
            {"candidate_id": "cand-good", "experiment_id": "exp-good", "cycles_observed": 2, "shadow_runs": 2, "avg_slippage_delta": 1.2, "avg_fill_quality": 0.03},
            {"candidate_id": "cand-bad", "experiment_id": "exp-bad", "cycles_observed": 1, "shadow_runs": 1, "avg_slippage_delta": 12.5, "avg_fill_quality": -0.4},
        ]
    }
    risk = {
        "items": [
            {"candidate_id": "cand-good", "experiment_id": "exp-good", "allowed": True, "kill_switch": False},
            {"candidate_id": "cand-bad", "experiment_id": "exp-bad", "allowed": False, "kill_switch": True},
        ]
    }
    admission = build_admission_gate(
        cycle_id="2026-04-05",
        candidate_lifecycle_index=lifecycle_index,
        candidate_retention_report=retention,
        risk_guard_report=risk,
    )
    assert any(x["admission_status"] == "approved" for x in admission["items"])
    assert any(x["admission_status"] == "rejected" for x in admission["items"])

    live_execution = {
        "items": [
            {"candidate_id": "cand-good", "experiment_id": "exp-good", "execution_status": "completed", "fill_rate": 0.82, "avg_slippage_bps": 6.5, "avg_latency_ms": 450},
            {"candidate_id": "cand-bad", "experiment_id": "exp-bad", "execution_status": "blocked_by_controls", "fill_rate": 0.0, "avg_slippage_bps": 0.0, "avg_latency_ms": 0.0},
        ]
    }
    live_feedback = {
        "items": [
            {"candidate_id": "cand-good", "experiment_id": "exp-good", "realized_pnl": 30, "risk_events": ["minor"]},
            {"candidate_id": "cand-bad", "experiment_id": "exp-bad", "realized_pnl": -120, "risk_events": ["kill", "drawdown"]},
        ]
    }
    health = build_health_monitor_report(
        cycle_id="2026-04-05",
        live_execution_report=live_execution,
        live_feedback=live_feedback,
        risk_guard_report=risk,
    )
    assert any(x["health_status"] == "warning" for x in health["items"])
    assert any(x["health_status"] == "critical" for x in health["items"])

    halt = build_auto_halt_report(
        cycle_id="2026-04-05",
        health_monitor_report=health,
        admission_gate=admission,
    )
    assert halt["items"]
    assert any(x["action"] in {"pause_candidate", "flatten_candidate", "demote_candidate"} for x in halt["items"])
    assert any(x["action"] == "demote_candidate" for x in halt["items"])

    state_root = tmp_path / "state"
    existing = load_active_registry(state_root=state_root)
    updated = update_active_registry(
        cycle_id="2026-04-05",
        existing_registry=existing,
        admission_gate=admission,
        health_monitor_report=health,
        auto_halt_report=halt,
    )
    path = persist_active_registry(registry=updated["registry"], state_root=state_root)
    assert Path(path).exists()
    items = updated["registry"]["items"]
    assert any(x["status"] == "active" for x in items)
    assert any(x["status"] == "halted" for x in items)
    assert updated["report"]["transitions"]
    json.dumps(updated, ensure_ascii=False)
