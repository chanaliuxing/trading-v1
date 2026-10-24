from __future__ import annotations

from pathlib import Path

from scripts.ai_trader.research.trading_day_controller import build_trading_day_status
from scripts.ai_trader.research.ops_control_center import build_ops_control_center
from scripts.ai_trader.research.end_of_day_review import build_end_of_day_review
from scripts.ai_trader.research.weekly_candidate_review import build_weekly_candidate_review
from scripts.ai_trader.research.review_to_plan_bridge import build_review_plan_patch
from scripts.ai_trader.research.human_ops_summary import build_human_ops_summary


def test_trading_day_ops_center_and_eod_review(tmp_path: Path) -> None:
    day = build_trading_day_status(
        day_id="d1",
        pre_open_prep_report={"prep_status": "blocked"},
        intraday_batch_report={"batch_warnings": ["batch_latency_warning"], "batch_action": "halt_batch"},
        eod_batch_close_report={"summary": {"forced_flatten": 1}},
        output_dir=tmp_path,
    )
    assert Path(day["trading_day_status_json"]).exists()

    ops = build_ops_control_center(
        day_id="d1",
        live_batch_plan={"batch_id": "b1", "mode": "small_live", "candidates": [{"candidate_id": "c1"}]},
        live_batch_status={"batch_status": "ready"},
        intraday_batch_report={"batch_warnings": ["batch_latency_warning"], "items": [{"candidate_id": "c1", "actions": ["flatten_one", "halt_batch"]}]},
        live_ops_summary={"launched_candidates": ["c1"], "next_actions": ["review_halted_candidates"]},
        capital_governor_report={"items": [{"effective_capital": 100.0}]},
        operator_controls={"live_enabled": True},
        output_dir=tmp_path,
    )
    assert Path(ops["ops_control_center_json"]).exists()
    assert ops["ops_control_center"]["samples"]["normal_running"] is not None
    assert ops["ops_control_center"]["samples"]["halt_warning_case"] is not None

    eod = build_end_of_day_review(
        day_id="d1",
        live_batch_plan={"candidates": [{"candidate_id": "c1"}, {"candidate_id": "c2"}]},
        intraday_batch_report={"items": [{"candidate_id": "c1", "actions": ["continue_batch"]}, {"candidate_id": "c2", "actions": ["flatten_one"]}], "batch_warnings": ["w1"]},
        eod_batch_close_report={"candidate_outcomes": [{"candidate_id": "c1", "close_status": "closed"}, {"candidate_id": "c2", "close_status": "forced_flatten"}]},
        live_ops_summary={"total_orders": 2, "total_fills": 1, "pnl_summary": {"expected": 10, "realized": -2}},
        output_dir=tmp_path,
    )
    assert Path(eod["end_of_day_review_json"]).exists()
    suggestions = {x.get("suggestion") for x in eod["end_of_day_review"]["candidate_outcomes"]}
    assert "retain_candidate" in suggestions
    assert "demote_candidate" in suggestions


def test_weekly_review_bridge_and_human_summary(tmp_path: Path) -> None:
    weekly = build_weekly_candidate_review(
        week_id="w1",
        candidate_lifecycle_index={"candidates": [{"candidate_id": "c1", "current_state": "paper_active"}, {"candidate_id": "c2", "current_state": "paper_active"}]},
        candidate_retention_report={"items": [{"candidate_id": "c1", "cycles_observed": 3}, {"candidate_id": "c2", "cycles_observed": 3}]},
        live_feedback_index={"items": [{"candidate_id": "c1", "feedback_status": "execution_ok"}, {"candidate_id": "c1", "feedback_status": "execution_ok"}, {"candidate_id": "c2", "feedback_status": "slippage_bad"}, {"candidate_id": "c2", "feedback_status": "fill_quality_bad"}, {"candidate_id": "c2", "feedback_status": "slippage_bad"}]},
        output_dir=tmp_path,
    )
    assert Path(weekly["weekly_candidate_review_json"]).exists()
    review = weekly["weekly_candidate_review"]
    assert review["promote"]
    assert review["demote"]
    assert review["retire"]
    assert review["watchlist"]

    eod = {
        "candidate_outcomes": [
            {"candidate_id": "c1", "suggestion": "retain_candidate"},
            {"candidate_id": "c2", "suggestion": "demote_candidate"},
        ]
    }
    patch = build_review_plan_patch(
        day_id="d2",
        end_of_day_review=eod,
        weekly_candidate_review=review,
        output_dir=tmp_path,
    )
    assert Path(patch["review_plan_patch_json"]).exists()
    actions = patch["review_plan_patch"]["actions"]
    assert any(x.get("patch_type") == "retire_candidate" for x in actions)
    assert any(x.get("patch_type") == "tighten_constraints" for x in actions)

    human = build_human_ops_summary(
        day_id="d2",
        trading_day_status={"current_stage": "archived"},
        ops_control_center={"system_mode": "small_live", "live_enabled": True, "active_batches": ["b1"], "active_candidates": ["c1"], "warnings": ["w1"], "halts": ["c2"]},
        end_of_day_review={**eod, "orders_summary": {"total_orders": 2}, "fills_summary": {"total_fills": 1}},
        weekly_candidate_review=review,
        review_plan_patch=patch["review_plan_patch"],
        output_dir=tmp_path,
    )
    assert Path(human["human_ops_summary_json"]).exists()
    payload = human["human_ops_summary"]
    assert "today_status" in payload
    assert "what_changed_for_tomorrow" in payload

