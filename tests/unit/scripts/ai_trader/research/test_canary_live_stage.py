from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.manual_approval import persist_manual_approval, build_manual_approval_report
from scripts.ai_trader.research.canary_launch_gate import build_canary_launch_gate
from scripts.ai_trader.research.canary_executor import run_canary_executor
from scripts.ai_trader.research.intraday_monitor import build_intraday_monitor_report
from scripts.ai_trader.research.post_trade_attribution import build_post_trade_attribution
from scripts.ai_trader.research.canary_feedback import apply_canary_feedback


def test_canary_gate_approval_and_rejection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    persist_manual_approval(
        {
            "approvals": [
                {
                    "candidate_id": "cand-a",
                    "approved": True,
                    "approved_by": "tester",
                    "approved_at": "2026-04-05T10:00:00+00:00",
                    "max_live_capital": 100.0,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "notes": "ok",
                }
            ]
        },
        state_root=state,
    )
    manual = build_manual_approval_report(
        cycle_id="c1",
        candidate_id="cand-a",
        output_dir=tmp_path,
        state_root=state,
    )["manual_approval_report"]
    gate_ok = build_canary_launch_gate(
        cycle_id="c1",
        candidate_id="cand-a",
        launch_verdict={"verdict": "go_small_live"},
        broker_safety_gate={"gate_status": "approved"},
        broker_launch_barrier={"barrier_status": "passed"},
        operator_controls={"live_enabled": True, "paper_only": False},
        broker_launch_switch={"real_broker_enabled": True, "real_broker_dry_run_only": False},
        manual_approval_report=manual,
        candidate_lifecycle_index={"candidates": [{"candidate_id": "cand-a", "current_state": "paper_active"}]},
        output_dir=tmp_path,
    )["canary_launch_gate"]
    assert gate_ok["gate_status"] == "approved"

    gate_bad = build_canary_launch_gate(
        cycle_id="c1",
        candidate_id="cand-b",
        launch_verdict={"verdict": "hold_live"},
        broker_safety_gate={"gate_status": "rejected"},
        broker_launch_barrier={"barrier_status": "blocked"},
        operator_controls={"live_enabled": False, "paper_only": True},
        broker_launch_switch={"real_broker_enabled": False, "real_broker_dry_run_only": True},
        manual_approval_report={"status": "rejected"},
        candidate_lifecycle_index={"candidates": [{"candidate_id": "cand-b", "current_state": "research_only"}]},
        output_dir=tmp_path,
    )["canary_launch_gate"]
    assert gate_bad["gate_status"] == "rejected"


def test_canary_execution_monitor_attribution_feedback(tmp_path: Path) -> None:
    canary = run_canary_executor(
        cycle_id="c2",
        candidate_id="cand-a",
        symbol="SPY",
        canary_launch_gate={"gate_status": "approved"},
        manual_approval_report={"manual_approval": {"max_live_capital": 100.0}},
        output_dir=tmp_path,
    )
    assert Path(canary["canary_run_summary_json"]).exists()
    summary = canary["canary_run_summary"]
    assert summary["execution_status"] == "executed"

    blocked = run_canary_executor(
        cycle_id="c2",
        candidate_id="cand-b",
        symbol="QQQ",
        canary_launch_gate={"gate_status": "rejected", "reasons": ["manual_approval_missing"]},
        manual_approval_report={"manual_approval": {}},
        output_dir=tmp_path,
    )
    assert blocked["canary_run_summary"]["execution_status"] == "blocked"

    monitor = build_intraday_monitor_report(
        cycle_id="c2",
        canary_run_summary=summary,
        output_dir=tmp_path,
    )["intraday_monitor_report"]
    assert monitor["warning_sample"]["type"] == "slippage_spike"
    assert monitor["halt_sample"]["action"] == "halt_canary"

    attribution = build_post_trade_attribution(
        cycle_id="c2",
        canary_run_summary=summary,
        intraday_monitor_report=monitor,
        output_dir=tmp_path,
    )["post_trade_attribution"]
    assert "expected_vs_realized_pnl" in attribution
    assert "execution_dragged_result" in attribution

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    lifecycle = {"candidates": [{"candidate_id": "cand-a", "current_state": "paper_active"}]}
    active = {"items": [{"candidate_id": "cand-a", "status": "active"}]}
    launch = {"last_verdict": "go_small_live"}
    system = {"live_enabled": True, "kill_switch_active": False}
    feedback = apply_canary_feedback(
        cycle_id="c2",
        candidate_id="cand-a",
        post_trade_attribution=attribution,
        intraday_monitor_report={"critical": ["pnl_drawdown"], "actions": ["halt_canary"]},
        candidate_lifecycle_index=lifecycle,
        active_registry=active,
        launch_registry=launch,
        system_status=system,
        state_root=state,
        output_dir=tmp_path,
    )["canary_feedback_report"]
    assert feedback["feedback_action"] in {"demote_or_pause", "keep_or_promote", "tighten_constraints"}
    assert Path(tmp_path / "canary_feedback_report.json").exists()
    persisted_active = json.loads((state / "active_registry.json").read_text(encoding="utf-8"))
    assert persisted_active["items"][0]["status"] in {"active", "inactive"}

