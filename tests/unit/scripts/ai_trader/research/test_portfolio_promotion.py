from __future__ import annotations

from scripts.ai_trader.research.portfolio_selection import build_portfolio_selection
from scripts.ai_trader.research.promotion_gate import build_promotion_gate
from scripts.ai_trader.research.shadow_bundle import build_allocation_plan


def test_portfolio_selection_rejects_by_correlation_and_allocates() -> None:
    bundle = {
        "leaderboard": [
            {
                "experiment_id": "exp-a",
                "score": 2.0,
                "quality_gate": {"passed": True},
                "result": {"market": "stocks", "strategy_family": "fa"},
            },
            {
                "experiment_id": "exp-b",
                "score": 1.8,
                "quality_gate": {"passed": True},
                "result": {"market": "stocks", "strategy_family": "fb"},
            },
            {
                "experiment_id": "exp-c",
                "score": 1.4,
                "quality_gate": {"passed": True},
                "result": {"market": "options", "strategy_family": "fc"},
            },
        ],
        "portfolio_evaluation": {"pairwise_correlation": 0.8},
    }
    selection = build_portfolio_selection(evaluation_bundle=bundle, candidate_pool={"candidates": []}, max_selected=2)
    assert len(selection["selected_candidates"]) >= 2
    assert any(r.get("reason") == "correlation_exceeded" for r in selection["rejected_candidates"])

    allocation = build_allocation_plan(portfolio_selection=selection, risk_budget_bps_total=100.0, capital_total=100_000.0)
    items = allocation["items"]
    assert len(items) >= 2
    assert items[0]["target_weight"] != items[1]["target_weight"]
    assert round(sum(x["target_weight"] for x in items), 4) <= 1.0


def test_promotion_gate_blocks_non_robust_and_non_selected() -> None:
    selection = {"selected_candidates": ["exp-ok"]}
    candidate_results = [
        {"experiment_id": "exp-ok", "status": "completed", "robustness": {"ok": True}},
        {"experiment_id": "exp-bad-robustness", "status": "completed", "robustness": {"ok": False}},
        {"experiment_id": "exp-blocked", "status": "blocked", "robustness": {"ok": True}},
    ]
    gate = build_promotion_gate(candidate_results=candidate_results, portfolio_selection=selection)
    rows = {row["experiment_id"]: row for row in gate["items"]}
    assert rows["exp-ok"]["promotion_target"] in {"paper_candidate", "shadow_candidate"}
    assert rows["exp-bad-robustness"]["promotion_target"] == "research_only"
    assert rows["exp-blocked"]["promotion_target"] == "research_only"


def test_promotion_gate_requires_in_cycle_result_even_if_selected() -> None:
    selection = {"selected_candidates": ["exp-selected-only"]}
    candidate_results = []
    gate = build_promotion_gate(candidate_results=candidate_results, portfolio_selection=selection)
    rows = {row["experiment_id"]: row for row in gate["items"]}
    assert rows["exp-selected-only"]["gate_status"] == "rejected"
    assert rows["exp-selected-only"]["promotion_target"] == "research_only"
    assert "missing_cycle_result" in rows["exp-selected-only"]["gate_reasons"]
