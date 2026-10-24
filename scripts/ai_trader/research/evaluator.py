from __future__ import annotations

"""Evaluator utilities for the research factory.

The evaluator consumes normalized experiment results and produces:
- quality gate verdicts
- score components
- promotion decisions
- MVP portfolio evaluation summaries
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from itertools import combinations
from math import tanh
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence
import uuid

from scripts.ai_trader.research.robustness import DEFAULT_REQUIRED_CHECKS, normalize_robustness_result


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if out != out or out in (float("inf"), float("-inf")):
            return default
        return out
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _extract_summary_metrics(experiment_result: Mapping[str, Any]) -> Dict[str, Any]:
    result = _as_dict(experiment_result)
    summary = _as_dict(result.get("summary_metrics"))
    if summary:
        return summary
    return result


def _extract_robustness(experiment_result: Mapping[str, Any]) -> Dict[str, Any]:
    result = _as_dict(experiment_result)
    if "robustness" in result:
        robustness = _as_dict(result.get("robustness"))
        if robustness:
            return robustness
    return normalize_robustness_result({}, result).to_dict()


@dataclass(frozen=True)
class QualityGateResult:
    passed: bool
    reasons: tuple[str, ...]
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


@dataclass(frozen=True)
class LeaderboardEntry:
    experiment_id: str
    score: float
    quality_passed: bool
    rank: int
    components: Dict[str, float] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass(frozen=True)
class PromotionDecision:
    cycle_id: str
    decision_id: str
    winners: list[Dict[str, Any]]
    retest_queue: list[Dict[str, Any]]
    rejected: list[Dict[str, Any]]
    blocked_by_data: list[Dict[str, Any]] = field(default_factory=list)
    blocked_by_robustness: list[Dict[str, Any]] = field(default_factory=list)
    not_evaluable: list[Dict[str, Any]] = field(default_factory=list)
    leaderboard: list[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass(frozen=True)
class PortfolioEvaluation:
    cycle_id: str
    candidate_set: tuple[str, ...]
    pairwise_correlation: float
    marginal_risk_contribution: Dict[str, float]
    capacity_fit: str
    drawdown_overlap_score: float
    diversification_score: float
    notes: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["candidate_set"] = list(self.candidate_set)
        payload["notes"] = list(self.notes)
        return payload


def compute_quality_gates(
    experiment_result: Mapping[str, Any],
    spec: Mapping[str, Any] | None = None,
) -> QualityGateResult:
    result = _as_dict(experiment_result)
    spec_map = _as_dict(spec)
    summary = _extract_summary_metrics(result)
    robustness = _extract_robustness(result)
    acceptance = _as_dict(spec_map.get("acceptance"))

    metrics = {
        "trades": _safe_int(summary.get("trades") or summary.get("trade_count")),
        "win_rate": _safe_float(summary.get("win_rate") or summary.get("win_rate_pct") or 0.0),
        "net_pnl": _safe_float(summary.get("net_pnl") or summary.get("pnl") or 0.0),
        "sharpe": _safe_float(summary.get("sharpe") or summary.get("sharpe_ratio") or 0.0),
        "max_drawdown_pct": _safe_float(
            summary.get("max_drawdown_pct")
            or summary.get("max_dd_pct")
            or summary.get("drawdown_pct")
            or 0.0
        ),
        "robustness": robustness,
    }

    min_trades = _safe_int(acceptance.get("min_trades"), 1)
    max_drawdown_pct = _safe_float(acceptance.get("max_drawdown_pct"), 100.0)
    min_sharpe = acceptance.get("min_sharpe")
    min_sharpe_value = _safe_float(min_sharpe, None) if min_sharpe is not None else None

    reasons: list[str] = []
    if metrics["trades"] < min_trades:
        reasons.append("min_trades_not_met")
    if metrics["max_drawdown_pct"] > max_drawdown_pct:
        reasons.append("max_drawdown_exceeded")
    if min_sharpe_value is not None and metrics["sharpe"] < min_sharpe_value:
        reasons.append("min_sharpe_not_met")

    if not robustness.get("ok", False):
        reasons.append("robustness_failed")
    if not robustness.get("wf_pass", False):
        reasons.append("walk_forward_failed")
    if not robustness.get("embargo_pass", True):
        reasons.append("embargo_failed")

    leakage_score = robustness.get("leakage_score")
    leakage_max = _safe_float(_as_dict(spec_map.get("robustness")).get("leakage_score_max"), 0.05)
    if leakage_score is not None and _safe_float(leakage_score, 1.0) > leakage_max:
        reasons.append("leakage_score_too_high")

    regime_min = _safe_float(_as_dict(spec_map.get("robustness")).get("regime_stability_min"), 0.5)
    regime_score = robustness.get("regime_stability")
    if regime_score is not None and _safe_float(regime_score, 0.0) < regime_min:
        reasons.append("regime_stability_too_low")

    transfer_min = _safe_float(_as_dict(spec_map.get("robustness")).get("symbol_transfer_min"), 0.5)
    transfer_score = robustness.get("symbol_transfer_score")
    if transfer_score is not None and _safe_float(transfer_score, 0.0) < transfer_min:
        reasons.append("symbol_transfer_too_low")

    return QualityGateResult(passed=not reasons, reasons=tuple(dict.fromkeys(reasons)), metrics=metrics)


def compute_complexity_penalty(spec: Mapping[str, Any] | None) -> float:
    spec_map = _as_dict(spec)
    module_selection = _as_dict(spec_map.get("module_selection"))
    parameters = _as_dict(spec_map.get("parameters"))
    robustness = _as_dict(spec_map.get("robustness"))
    lineage = _as_dict(spec_map.get("lineage"))

    feature_count = len(_as_str_list(module_selection.get("features")))
    gate_count = len(_as_str_list(module_selection.get("gates")))
    param_count = len(parameters)
    required_checks = len(_as_str_list(robustness.get("required_checks")) or list(DEFAULT_REQUIRED_CHECKS))
    lineage_depth = 1 if lineage.get("parent_experiment_id") else 0

    penalty = (
        feature_count * 1.2
        + gate_count * 0.9
        + min(param_count, 12) * 0.4
        + required_checks * 0.7
        + lineage_depth * 0.25
    )
    return round(min(25.0, penalty), 4)


_LATENCY_PENALTY = {
    "sub_1s": 0.0,
    "sub_2s": 0.5,
    "sub_5s": 1.0,
    "slow": 2.0,
    "unknown": 0.75,
}


def normalize_costs(
    experiment_result: Mapping[str, Any],
    spec: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    result = _as_dict(experiment_result)
    spec_map = _as_dict(spec)
    exec_reality = _as_dict(result.get("execution_reality"))
    spec_exec = _as_dict(spec_map.get("execution_reality"))

    modeled_slippage_bps = _safe_float(
        exec_reality.get("modeled_slippage_bps")
        or exec_reality.get("slippage_bps")
        or spec_exec.get("modeled_slippage_bps")
        or spec_exec.get("slippage_bps"),
        0.0,
    )
    spread_cost_bps = _safe_float(
        exec_reality.get("spread_cost_bps")
        or spec_exec.get("spread_cost_bps"),
        0.0,
    )
    commission_bps = _safe_float(
        exec_reality.get("commission_bps")
        or spec_exec.get("commission_bps"),
        0.0,
    )
    fill_ratio = _clamp(
        _safe_float(exec_reality.get("fill_ratio") or spec_exec.get("fill_ratio"), 1.0),
        0.0,
        1.0,
    )
    partial_fill_rate = _clamp(
        _safe_float(exec_reality.get("partial_fill_rate") or spec_exec.get("partial_fill_rate"), 0.0),
        0.0,
        1.0,
    )
    latency_bucket = str(
        exec_reality.get("latency_bucket")
        or spec_exec.get("latency_bucket")
        or "unknown"
    ).strip().lower()
    latency_penalty_bps = _LATENCY_PENALTY.get(latency_bucket, _LATENCY_PENALTY["unknown"])

    cost_bps = modeled_slippage_bps + spread_cost_bps + commission_bps + latency_penalty_bps
    estimated_cost_penalty = round(cost_bps + partial_fill_rate * 5.0, 4)

    return {
        "modeled_slippage_bps": round(modeled_slippage_bps, 4),
        "spread_cost_bps": round(spread_cost_bps, 4),
        "commission_bps": round(commission_bps, 4),
        "fill_ratio": round(fill_ratio, 4),
        "partial_fill_rate": round(partial_fill_rate, 4),
        "latency_bucket": latency_bucket,
        "latency_penalty_bps": round(latency_penalty_bps, 4),
        "effective_cost_bps": round(cost_bps, 4),
        "estimated_cost_penalty": estimated_cost_penalty,
    }


def _robustness_score(robustness: Mapping[str, Any]) -> float:
    if not robustness:
        return 0.0
    passed = _as_str_list(robustness.get("passed_checks"))
    failed = _as_str_list(robustness.get("failed_checks"))
    total = len(passed) + len(failed)
    if total == 0:
        if robustness.get("ok", False):
            return 1.0
        return 0.0
    return round(len(passed) / total, 4)


def score_experiment(
    experiment_result: Mapping[str, Any],
    spec: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    result = _as_dict(experiment_result)
    spec_map = _as_dict(spec)
    summary = _extract_summary_metrics(result)
    robustness = _extract_robustness(result)
    gates = compute_quality_gates(result, spec_map)
    costs = normalize_costs(result, spec_map)
    complexity_penalty = compute_complexity_penalty(spec_map)

    sharpe = _safe_float(summary.get("sharpe") or summary.get("sharpe_ratio"), 0.0)
    win_rate = _safe_float(summary.get("win_rate") or summary.get("win_rate_pct"), 0.0)
    net_pnl = _safe_float(summary.get("net_pnl") or summary.get("pnl"), 0.0)
    pnl_component = tanh(net_pnl / 5_000.0) * 15.0
    sharpe_component = tanh(sharpe / 3.0) * 30.0
    win_rate_component = (win_rate - 0.5) * 20.0
    robustness_component = _robustness_score(robustness) * 10.0
    quality_bonus = 10.0 if gates.passed else -10.0
    cost_penalty = costs["estimated_cost_penalty"] * 0.5

    score = round(
        sharpe_component
        + pnl_component
        + win_rate_component
        + robustness_component
        + quality_bonus
        - complexity_penalty
        - cost_penalty,
        4,
    )

    components = {
        "sharpe_component": round(sharpe_component, 4),
        "pnl_component": round(pnl_component, 4),
        "win_rate_component": round(win_rate_component, 4),
        "robustness_component": round(robustness_component, 4),
        "quality_bonus": round(quality_bonus, 4),
        "complexity_penalty": round(complexity_penalty, 4),
        "cost_penalty": round(cost_penalty, 4),
    }

    experiment_id = str(result.get("experiment_id") or spec_map.get("experiment_id") or "")
    return {
        "experiment_id": experiment_id,
        "score": score,
        "quality_gate": gates.to_dict(),
        "costs": costs,
        "robustness_score": round(_robustness_score(robustness), 4),
        "complexity_penalty": round(complexity_penalty, 4),
        "components": components,
        "metrics": {
            "sharpe": sharpe,
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "trades": gates.metrics["trades"],
        },
        "result": result,
    }


def rank_candidates(
    candidate_results: Sequence[Mapping[str, Any]],
    *,
    specs_by_experiment_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Dict[str, Any]]:
    specs_by_experiment_id = _as_dict(specs_by_experiment_id)
    leaderboard = [
        score_experiment(
            candidate,
            specs_by_experiment_id.get(str(_as_dict(candidate).get("experiment_id") or "")),
        )
        for candidate in candidate_results
    ]
    leaderboard.sort(key=lambda item: item["score"], reverse=True)
    for idx, entry in enumerate(leaderboard, start=1):
        entry["rank"] = idx
    return leaderboard


def build_promotion_decision(
    leaderboard: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str | None = None,
    top_k: int = 1,
) -> PromotionDecision:
    cycle = str(cycle_id or "adhoc").strip()
    passing = [row for row in leaderboard if _as_dict(row).get("quality_gate", {}).get("passed")]
    if passing:
        winners = [
            {
                "experiment_id": str(row.get("experiment_id") or ""),
                "tier": "candidate",
                "score": _safe_float(row.get("score"), 0.0),
            }
            for row in passing[:top_k]
        ]
        retest_queue = [
            {
                "experiment_id": str(row.get("experiment_id") or ""),
                "reason": "quality_gate_failed",
                "score": _safe_float(row.get("score"), 0.0),
            }
            for row in leaderboard
            if not _as_dict(row).get("quality_gate", {}).get("passed")
        ][: max(0, top_k)]
    else:
        winners = []
        retest_queue = [
            {
                "experiment_id": str(row.get("experiment_id") or ""),
                "reason": "needs_retest",
                "score": _safe_float(row.get("score"), 0.0),
            }
            for row in leaderboard[: max(1, top_k)]
        ]

    rejected = []
    blocked_by_data: list[Dict[str, Any]] = []
    blocked_by_robustness: list[Dict[str, Any]] = []
    not_evaluable: list[Dict[str, Any]] = []
    winner_ids = {item["experiment_id"] for item in winners}
    retest_ids = {item["experiment_id"] for item in retest_queue}
    for row in leaderboard:
        row_map = _as_dict(row)
        exp_id = str(row.get("experiment_id") or "")
        if exp_id in winner_ids or exp_id in retest_ids:
            continue
        category = _classify_outcome_category(row_map)
        item = {
            "experiment_id": exp_id,
            "reason": "not_selected",
            "score": _safe_float(row.get("score"), 0.0),
            "category": category,
        }
        if category == "blocked_by_data":
            blocked_by_data.append(item)
            continue
        if category == "blocked_by_robustness":
            blocked_by_robustness.append(item)
            continue
        if category == "not_evaluable":
            not_evaluable.append(item)
            continue
        rejected.append(
            item
        )

    decision_id = f"promote-{cycle}-{uuid.uuid4().hex[:8]}"
    return PromotionDecision(
        cycle_id=cycle,
        decision_id=decision_id,
        winners=winners,
        retest_queue=retest_queue,
        rejected=rejected,
        blocked_by_data=blocked_by_data,
        blocked_by_robustness=blocked_by_robustness,
        not_evaluable=not_evaluable,
        leaderboard=[dict(row) for row in leaderboard],
    )


def _classify_outcome_category(row: Mapping[str, Any]) -> str:
    row_map = _as_dict(row)
    result = _as_dict(row_map.get("result"))
    reasons = set(_as_str_list(_as_dict(row_map.get("quality_gate")).get("reasons")))
    status = str(result.get("status") or "").strip().lower()
    notes_text = " ".join(_as_str_list(result.get("notes"))).lower()
    blocked_by = _as_dict(result.get("blocked_by"))

    if blocked_by.get("type") == "data_readiness":
        return "blocked_by_data"
    if any(token in notes_text for token in ("missing", "coverage", "bars_missing", "readiness", "date_window")):
        return "blocked_by_data"
    if status in {"blocked", "failed"} and not reasons:
        return "not_evaluable"
    if {"robustness_failed", "walk_forward_failed", "embargo_failed"} & reasons:
        return "blocked_by_robustness"
    return "rejected"


def build_blocked_summary(leaderboard: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_market: Dict[str, int] = {}
    by_symbol: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    by_failure_type: Dict[str, int] = {}
    blocked_total = 0

    for row in leaderboard:
        row_map = _as_dict(row)
        result = _as_dict(row_map.get("result"))
        quality = _as_dict(row_map.get("quality_gate"))
        reasons = _as_str_list(quality.get("reasons"))
        if quality.get("passed"):
            continue
        blocked_total += 1
        market = str(result.get("market") or "unknown")
        by_market[market] = by_market.get(market, 0) + 1
        symbols = _as_str_list(_as_dict(result.get("universe")).get("symbols"))
        if not symbols:
            symbols = _as_str_list(_as_dict(_as_dict(result.get("spec")).get("universe")).get("symbols"))
        for sym in symbols or ["unknown"]:
            by_symbol[sym] = by_symbol.get(sym, 0) + 1
        for reason in reasons or ["unknown_quality_gate_reason"]:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        robustness = _as_dict(result.get("robustness"))
        failure_type = str(robustness.get("failure_type") or "").strip() or "unknown"
        by_failure_type[failure_type] = by_failure_type.get(failure_type, 0) + 1

    return {
        "blocked_total": blocked_total,
        "by_market": dict(sorted(by_market.items())),
        "by_symbol": dict(sorted(by_symbol.items())),
        "by_failure_reason": dict(sorted(by_reason.items())),
        "by_robustness_failure_type": dict(sorted(by_failure_type.items())),
    }


def build_quality_gate_breakdown(leaderboard: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    totals = {
        "total": len(list(leaderboard)),
        "passed": 0,
        "failed": 0,
    }
    reasons: Dict[str, int] = {}
    for row in leaderboard:
        quality = _as_dict(_as_dict(row).get("quality_gate"))
        if quality.get("passed"):
            totals["passed"] += 1
            continue
        totals["failed"] += 1
        for reason in _as_str_list(quality.get("reasons")):
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        **totals,
        "failure_reasons": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
    }


def build_next_cycle_recommendations(
    blocked_summary: Mapping[str, Any],
    quality_breakdown: Mapping[str, Any],
) -> Dict[str, Any]:
    blocked = _as_dict(blocked_summary)
    reasons = _as_dict(quality_breakdown).get("failure_reasons") or {}
    reason_counts = dict(reasons) if isinstance(reasons, Mapping) else {}
    actions: list[Dict[str, Any]] = []

    if reason_counts.get("min_trades_not_met", 0) > 0:
        actions.append({"action": "window_expand", "reason": "min_trades_not_met", "priority": "high"})
    if reason_counts.get("walk_forward_failed", 0) > 0:
        actions.append({"action": "increase_folds_or_resegment", "reason": "walk_forward_failed", "priority": "high"})
    if reason_counts.get("leakage_score_too_high", 0) > 0:
        actions.append({"action": "feature_leakage_audit", "reason": "leakage_score_too_high", "priority": "high"})
    if reason_counts.get("regime_stability_too_low", 0) > 0:
        actions.append({"action": "regime_split_retest", "reason": "regime_stability_too_low", "priority": "medium"})

    by_market = _as_dict(blocked.get("by_market"))
    if by_market.get("options", 0) and by_market.get("stocks", 0):
        actions.append({"action": "cross_asset_rebalance", "reason": "both_markets_blocked", "priority": "medium"})

    if not actions:
        actions.append({"action": "promote_winner_and_monitor", "reason": "quality_gate_healthy", "priority": "low"})

    return {"actions": actions}


def _extract_pairwise_correlation(candidate: Mapping[str, Any]) -> float | None:
    result = _as_dict(candidate)
    sources = [
        _as_dict(result.get("portfolio_metrics")),
        _as_dict(result.get("evaluation")),
        _as_dict(result.get("metrics")),
        result,
    ]
    for data in sources:
        for key in ("pairwise_correlation", "correlation_to_book", "correlation", "avg_pairwise_correlation"):
            if key in data and data.get(key) is not None:
                return _safe_float(data.get(key), None)
    return None


def _extract_drawdown_overlap(candidate: Mapping[str, Any]) -> float | None:
    result = _as_dict(candidate)
    sources = [
        _as_dict(result.get("portfolio_metrics")),
        _as_dict(result.get("evaluation")),
        result,
    ]
    for data in sources:
        for key in ("drawdown_overlap_score", "dd_overlap_score"):
            if key in data and data.get(key) is not None:
                return _safe_float(data.get(key), None)
    return None


def build_portfolio_evaluation(
    candidate_results: Sequence[Mapping[str, Any]],
    *,
    leaderboard: Sequence[Mapping[str, Any]] | None = None,
    cycle_id: str | None = None,
) -> PortfolioEvaluation:
    candidate_list = [str(_as_dict(item).get("experiment_id") or "") for item in candidate_results]
    cycle = str(cycle_id or "adhoc").strip()
    leaderboard = leaderboard or rank_candidates(candidate_results)

    pairwise_values: list[float] = []
    drawdown_values: list[float] = []
    risk_inputs: Dict[str, float] = {}
    capacity_fit = "unknown"
    notes: list[str] = []

    for candidate in candidate_results:
        item = _as_dict(candidate)
        exp_id = str(item.get("experiment_id") or "")
        summary = _extract_summary_metrics(item)
        score_entry = next((row for row in leaderboard if str(_as_dict(row).get("experiment_id") or "") == exp_id), None)

        corr = _extract_pairwise_correlation(item)
        if corr is not None:
            pairwise_values.append(abs(corr))

        dd_overlap = _extract_drawdown_overlap(item)
        if dd_overlap is not None:
            drawdown_values.append(_clamp(dd_overlap, 0.0, 1.0))

        max_dd = _safe_float(summary.get("max_drawdown_pct") or summary.get("max_dd_pct"), 0.0)
        cost_penalty = _safe_float(_as_dict(score_entry).get("costs", {}).get("estimated_cost_penalty"), 0.0)
        risk_inputs[exp_id] = round(max(0.0, max_dd) + max(0.0, cost_penalty), 4)

        if item.get("capacity_limited") or _as_dict(item.get("portfolio_metrics")).get("capacity_limited"):
            capacity_fit = "constrained"
        elif capacity_fit != "constrained" and (item.get("capacity_usd") or _as_dict(item.get("portfolio_metrics")).get("capacity_usd")):
            capacity_fit = "ok"

        if not item.get("capacity_usd") and not _as_dict(item.get("portfolio_metrics")).get("capacity_usd"):
            notes.append(f"{exp_id}:capacity_unknown")

    pairwise_corr = round(sum(pairwise_values) / len(pairwise_values), 4) if pairwise_values else 0.0
    drawdown_overlap = round(sum(drawdown_values) / len(drawdown_values), 4) if drawdown_values else 0.0
    diversification_score = round(_clamp(1.0 - pairwise_corr, 0.0, 1.0) * 0.7 + _clamp(1.0 - drawdown_overlap, 0.0, 1.0) * 0.3, 4)

    total_risk = sum(risk_inputs.values())
    if total_risk > 0.0:
        marginal_risk_contribution = {
            exp_id: round(value / total_risk, 4) for exp_id, value in risk_inputs.items()
        }
    else:
        marginal_risk_contribution = {exp_id: 0.0 for exp_id in candidate_list}

    return PortfolioEvaluation(
        cycle_id=cycle,
        candidate_set=tuple(candidate_list),
        pairwise_correlation=pairwise_corr,
        marginal_risk_contribution=marginal_risk_contribution,
        capacity_fit=capacity_fit,
        drawdown_overlap_score=drawdown_overlap,
        diversification_score=diversification_score,
        notes=tuple(dict.fromkeys(notes)),
    )


def evaluate_candidates(
    candidate_results: Sequence[Mapping[str, Any]],
    *,
    specs_by_experiment_id: Mapping[str, Mapping[str, Any]] | None = None,
    cycle_id: str | None = None,
    top_k: int = 1,
) -> Dict[str, Any]:
    leaderboard = rank_candidates(candidate_results, specs_by_experiment_id=specs_by_experiment_id)
    promotion = build_promotion_decision(leaderboard, cycle_id=cycle_id, top_k=top_k)
    portfolio = build_portfolio_evaluation(candidate_results, leaderboard=leaderboard, cycle_id=cycle_id)
    blocked_summary = build_blocked_summary(leaderboard)
    quality_breakdown = build_quality_gate_breakdown(leaderboard)
    next_cycle_recommendations = build_next_cycle_recommendations(blocked_summary, quality_breakdown)
    return {
        "leaderboard": leaderboard,
        "promotion_decision": promotion.to_dict(),
        "portfolio_evaluation": portfolio.to_dict(),
        "blocked_summary": blocked_summary,
        "quality_gate_breakdown": quality_breakdown,
        "next_cycle_recommendations": next_cycle_recommendations,
    }


def persist_evaluation_bundle(
    candidate_results: Sequence[Mapping[str, Any]],
    *,
    output_dir: str | Path,
    specs_by_experiment_id: Mapping[str, Mapping[str, Any]] | None = None,
    cycle_id: str | None = None,
    top_k: int = 1,
) -> Dict[str, Any]:
    bundle = evaluate_candidates(
        candidate_results,
        specs_by_experiment_id=specs_by_experiment_id,
        cycle_id=cycle_id,
        top_k=top_k,
    )
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    leaderboard_path = output_path / "leaderboard.json"
    promotion_path = output_path / "promotion_decision.json"
    portfolio_path = output_path / "portfolio_evaluation.json"
    blocked_summary_path = output_path / "blocked_summary.json"
    quality_breakdown_path = output_path / "quality_gate_breakdown.json"
    next_cycle_path = output_path / "next_cycle_recommendations.json"
    bundle_path = output_path / "evaluation_bundle.json"

    leaderboard_path.write_text(json.dumps(bundle["leaderboard"], indent=2, ensure_ascii=False), encoding="utf-8")
    promotion_path.write_text(json.dumps(bundle["promotion_decision"], indent=2, ensure_ascii=False), encoding="utf-8")
    portfolio_path.write_text(json.dumps(bundle["portfolio_evaluation"], indent=2, ensure_ascii=False), encoding="utf-8")
    blocked_summary_path.write_text(json.dumps(bundle["blocked_summary"], indent=2, ensure_ascii=False), encoding="utf-8")
    quality_breakdown_path.write_text(json.dumps(bundle["quality_gate_breakdown"], indent=2, ensure_ascii=False), encoding="utf-8")
    next_cycle_path.write_text(json.dumps(bundle["next_cycle_recommendations"], indent=2, ensure_ascii=False), encoding="utf-8")
    bundle_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        **bundle,
        "artifacts": {
            "output_dir": str(output_path),
            "leaderboard_json": str(leaderboard_path),
            "promotion_decision_json": str(promotion_path),
            "portfolio_evaluation_json": str(portfolio_path),
            "blocked_summary_json": str(blocked_summary_path),
            "quality_gate_breakdown_json": str(quality_breakdown_path),
            "next_cycle_recommendations_json": str(next_cycle_path),
            "evaluation_bundle_json": str(bundle_path),
        },
    }


__all__ = [
    "LeaderboardEntry",
    "PortfolioEvaluation",
    "PromotionDecision",
    "QualityGateResult",
    "build_portfolio_evaluation",
    "build_promotion_decision",
    "compute_complexity_penalty",
    "compute_quality_gates",
    "evaluate_candidates",
    "build_blocked_summary",
    "build_quality_gate_breakdown",
    "build_next_cycle_recommendations",
    "persist_evaluation_bundle",
    "normalize_costs",
    "rank_candidates",
    "score_experiment",
]
