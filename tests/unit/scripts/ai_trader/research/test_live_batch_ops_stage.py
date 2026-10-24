from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.live_batch_orchestrator import build_live_batch_plan
from scripts.ai_trader.research.pre_open_prep import run_pre_open_prep
from scripts.ai_trader.research.intraday_batch_supervisor import run_intraday_batch_supervisor
from scripts.ai_trader.research.eod_batch_close import run_eod_batch_close
from scripts.ai_trader.research.live_ops_summary import build_live_ops_summary
from scripts.ai_trader.research.live_batch_feedback import apply_live_batch_feedback


def _launch_batch() -> dict:
    return {
        "batch_id": "batch-c1",
        "candidates": [
            {"candidate_id": "cand-a", "experiment_id": "exp-a", "max_capital": 600.0},
            {"candidate_id": "cand-b", "experiment_id": "exp-b", "max_capital": 500.0},
            {"candidate_id": "cand-c", "experiment_id": "exp-c", "max_capital": 400.0},
        ],
    }


def test_live_batch_plan_and_preopen_intraday_eod_summary(tmp_path: Path) -> None:
    plan_bundle = build_live_batch_plan(
        cycle_id="c1",
        launch_batch=_launch_batch(),
        admission_gate={
            "items": [
                {"candidate_id": "cand-a", "admission_status": "approved"},
                {"candidate_id": "cand-b", "admission_status": "approved"},
                {"candidate_id": "cand-c", "admission_status": "approved"},
            ]
        },
        capital_governor_report={
            "items": [
                {"candidate_id": "cand-a", "decision": "approved", "effective_capital": 600.0, "effective_risk_bps": 20.0},
                {"candidate_id": "cand-b", "decision": "approved", "effective_capital": 500.0, "effective_risk_bps": 20.0},
                {"candidate_id": "cand-c", "decision": "rejected", "effective_capital": 0.0, "effective_risk_bps": 0.0},
            ]
        },
        risk_guard_report={
            "items": [
                {"candidate_id": "cand-a", "allowed": True, "reasons": ["passed"]},
                {"candidate_id": "cand-b", "allowed": True, "reasons": ["passed"]},
                {"candidate_id": "cand-c", "allowed": False, "reasons": ["risk_guard_blocked"]},
            ]
        },
        mode="small_live",
        output_dir=tmp_path,
        max_batch_capital=1200.0,
    )
    plan = plan_bundle["live_batch_plan"]
    assert len(plan["candidates"]) >= 2
    assert plan["excluded_candidates"]

    prep_bundle = run_pre_open_prep(
        cycle_id="c1",
        live_batch_plan=plan,
        manual_approvals={
            "approvals": [
                {"candidate_id": "cand-a", "approved": True, "expires_at": "2099-01-01T00:00:00+00:00"},
                {"candidate_id": "cand-b", "approved": False, "expires_at": "2099-01-01T00:00:00+00:00"},
            ]
        },
        broker_readonly_probe={"status": "success"},
        account_snapshot={"account": {"cash": 10000}},
        positions_snapshot={"positions": []},
        operator_controls={"live_enabled": True, "global_pause": False},
        session_control_report={"items": [{"candidate_id": "cand-a", "session_allowed": True}, {"candidate_id": "cand-b", "session_allowed": True}]},
        kill_switch_report={"kill_switch_active": False},
        output_dir=tmp_path,
    )
    prep = prep_bundle["pre_open_prep_report"]
    assert prep["prep_status"] == "blocked"

    intraday_bundle = run_intraday_batch_supervisor(
        cycle_id="c1",
        live_batch_plan=plan,
        pre_open_prep_report=prep,
        risk_guard_report={
            "items": [
                {"candidate_id": "cand-a", "allowed": True, "reasons": ["passed"]},
                {"candidate_id": "cand-b", "allowed": False, "reasons": ["kill_switch_active"]},
            ]
        },
        output_dir=tmp_path,
    )
    intraday = intraday_bundle["intraday_batch_report"]
    assert intraday["batch_warnings"]
    assert intraday["single_candidate_halt_sample"]["action"] == "flatten_one"

    eod_bundle = run_eod_batch_close(
        cycle_id="c1",
        live_batch_plan=plan,
        intraday_batch_report=intraday,
        broker_reconcile_report={"clean_reconcile": False, "mismatches": [{"type": "status_mismatch"}]},
        output_dir=tmp_path,
    )
    eod = eod_bundle["eod_batch_close_report"]
    assert Path(eod_bundle["eod_batch_close_report_json"]).exists()
    assert eod["forced_flatten_sample"]["close_status"] == "forced_flatten"

    ops_bundle = build_live_ops_summary(
        cycle_id="c1",
        live_batch_plan=plan,
        pre_open_prep_report=prep,
        intraday_batch_report=intraday,
        eod_batch_close_report=eod,
        output_dir=tmp_path,
    )
    ops = ops_bundle["live_ops_summary"]
    assert "batch_id" in ops
    assert "pnl_summary" in ops
    assert Path(ops_bundle["live_ops_summary_json"]).exists()


def test_live_batch_feedback_selective_demote(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    feedback_bundle = apply_live_batch_feedback(
        cycle_id="c2",
        live_batch_plan={
            "batch_id": "batch-c2",
            "candidates": [{"candidate_id": "cand-a"}, {"candidate_id": "cand-b"}],
        },
        intraday_batch_report={
            "items": [
                {"candidate_id": "cand-a", "actions": ["continue_batch"]},
                {"candidate_id": "cand-b", "actions": ["flatten_one", "pause_one"]},
            ]
        },
        eod_batch_close_report={
            "candidate_outcomes": [
                {"candidate_id": "cand-a", "close_status": "closed"},
                {"candidate_id": "cand-b", "close_status": "forced_flatten"},
            ]
        },
        candidate_lifecycle_index={
            "candidates": [
                {"candidate_id": "cand-a", "current_state": "paper_active"},
                {"candidate_id": "cand-b", "current_state": "paper_active"},
            ]
        },
        active_registry={"items": [{"candidate_id": "cand-a", "status": "active"}, {"candidate_id": "cand-b", "status": "active"}]},
        launch_registry={"last_verdict": "go_small_live"},
        state_root=state_root,
        output_dir=tmp_path,
    )
    report = feedback_bundle["live_batch_feedback_report"]
    assert report["demoted_subset"]
    assert report["feedback_items"]
    assert Path(feedback_bundle["live_batch_feedback_report_json"]).exists()
    persisted = json.loads((state_root / "candidate_lifecycle_index.json").read_text(encoding="utf-8"))
    states = {x["candidate_id"]: x["current_state"] for x in persisted.get("candidates", [])}
    assert states["cand-b"] in {"demoted", "paper_active"}

