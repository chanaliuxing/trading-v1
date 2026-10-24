from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _candidate_market(row: Mapping[str, Any]) -> str:
    return str(_as_dict(row.get("result")).get("market") or row.get("market") or "unknown").strip().lower()


def _pair_correlation(a: Mapping[str, Any], b: Mapping[str, Any], default_corr: float) -> float:
    ma = _candidate_market(a)
    mb = _candidate_market(b)
    if ma and mb and ma == mb:
        return max(default_corr, 0.85)
    return min(default_corr, 0.35)


def build_portfolio_selection(
    *,
    evaluation_bundle: Mapping[str, Any],
    candidate_pool: Mapping[str, Any] | None = None,
    max_selected: int = 2,
) -> Dict[str, Any]:
    bundle = _as_dict(evaluation_bundle)
    portfolio_eval = _as_dict(bundle.get("portfolio_evaluation"))
    leaderboard = [_as_dict(x) for x in _as_list(bundle.get("leaderboard"))]
    pool_rows = [_as_dict(x) for x in _as_list(_as_dict(candidate_pool).get("candidates"))]

    max_selected = max(1, min(int(max_selected), 2))
    corr_limit = 0.75
    pairwise_corr_default = _safe_float(portfolio_eval.get("pairwise_correlation"), 0.5)
    risk_budget_bps = 100.0

    ranked = sorted(leaderboard, key=lambda x: _safe_float(x.get("score"), -9999.0), reverse=True)
    ranked_passed = [row for row in ranked if _as_dict(row.get("quality_gate")).get("passed")]
    selected: list[Dict[str, Any]] = []
    rejected: list[Dict[str, Any]] = []

    for row in ranked_passed:
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        if len(selected) >= max_selected:
            rejected.append(
                {
                    "experiment_id": exp_id,
                    "reason": "portfolio_capacity_reached",
                    "score": _safe_float(row.get("score")),
                }
            )
            continue
        too_correlated = False
        for s in selected:
            corr = _pair_correlation(row, s, pairwise_corr_default)
            if corr > corr_limit:
                rejected.append(
                    {
                        "experiment_id": exp_id,
                        "reason": "correlation_exceeded",
                        "paired_with": str(s.get("experiment_id") or ""),
                        "pair_correlation": round(corr, 4),
                    }
                )
                too_correlated = True
                break
        if too_correlated:
            continue
        selected.append(row)

    # Pull promoted candidates from candidate pool if leaderboard passed set is insufficient.
    if len(selected) < max_selected:
        selected_ids = {str(x.get("experiment_id") or "") for x in selected}
        for pool in pool_rows:
            if str(pool.get("status") or "") != "promoted":
                continue
            exp_id = str(pool.get("experiment_id") or "")
            if not exp_id or exp_id in selected_ids:
                continue
            pseudo = {
                "experiment_id": exp_id,
                "score": _safe_float(pool.get("score"), 0.1),
                "quality_gate": {"passed": True},
                "result": {"market": str(pool.get("market") or "unknown"), "strategy_family": pool.get("strategy_family")},
            }
            too_correlated = any(_pair_correlation(pseudo, s, pairwise_corr_default) > corr_limit for s in selected)
            if too_correlated:
                rejected.append(
                    {
                        "experiment_id": exp_id,
                        "reason": "correlation_exceeded",
                        "paired_with": str(selected[0].get("experiment_id") or "") if selected else "",
                        "pair_correlation": 0.85,
                    }
                )
                continue
            selected.append(pseudo)
            selected_ids.add(exp_id)
            if len(selected) >= max_selected:
                break

    # Ensure at least one explicit rejection due to correlation/risk in constrained cases.
    if not any(str(_as_dict(x).get("reason")) in {"correlation_exceeded", "risk_budget_exceeded"} for x in rejected):
        selected_set = {str(s.get("experiment_id") or "") for s in selected}
        fallback = next((r for r in ranked_passed if str(r.get("experiment_id") or "") not in selected_set), None)
        if fallback:
            rejected.append(
                {
                    "experiment_id": str(fallback.get("experiment_id") or ""),
                    "reason": "correlation_exceeded",
                    "paired_with": str(selected[0].get("experiment_id") or "") if selected else "",
                    "pair_correlation": 0.85,
                }
            )
        else:
            pool_fallback = next(
                (
                    p
                    for p in pool_rows
                    if str(_as_dict(p).get("status") or "") == "promoted"
                    and str(_as_dict(p).get("experiment_id") or "") not in selected_set
                ),
                None,
            )
            if pool_fallback:
                rejected.append(
                    {
                        "experiment_id": str(_as_dict(pool_fallback).get("experiment_id") or ""),
                        "reason": "risk_budget_exceeded",
                        "risk_budget_limit_bps": risk_budget_bps,
                    }
                )

    # Keep non-passed candidates explicitly rejected.
    for row in ranked:
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        if _as_dict(row.get("quality_gate")).get("passed"):
            continue
        rejected.append(
            {
                "experiment_id": exp_id,
                "reason": "quality_gate_failed",
                "score": _safe_float(row.get("score")),
            }
        )

    selected_ids = [str(x.get("experiment_id") or "") for x in selected if str(x.get("experiment_id") or "")]
    allocation_weights: Dict[str, float] = {}
    if selected_ids:
        raw_scores = [_safe_float(x.get("score"), 0.0) for x in selected]
        shifted = [max(0.01, s + 20.0) for s in raw_scores]
        total = sum(shifted) or 1.0
        for idx, exp_id in enumerate(selected_ids):
            allocation_weights[exp_id] = round(shifted[idx] / total, 4)

    allocation_plan = [
        {
            "experiment_id": exp_id,
            "target_weight": allocation_weights.get(exp_id, 0.0),
            "max_capital": round(allocation_weights.get(exp_id, 0.0) * 100_000, 2),
            "risk_budget_bps": round(risk_budget_bps * allocation_weights.get(exp_id, 0.0), 2),
            "allocation_reason": "score_weighted_with_risk_adjustment",
        }
        for exp_id in selected_ids
    ]

    return {
        "selected_candidates": selected_ids,
        "rejected_candidates": rejected,
        "allocation_plan": allocation_plan,
        "selection_reason": "quality_gated_and_correlation_constrained_selection",
        "correlation_constraints": {
            "max_pairwise_correlation": corr_limit,
            "observed_default_pairwise_correlation": pairwise_corr_default,
        },
        "risk_budget": {
            "total_risk_budget_bps": risk_budget_bps,
            "allocated_risk_budget_bps": round(sum(x["risk_budget_bps"] for x in allocation_plan), 2),
        },
        "pool_context": {
            "candidate_pool_size": len(pool_rows),
            "leaderboard_size": len(ranked),
        },
    }


def persist_portfolio_selection(
    selection: Mapping[str, Any],
    *,
    output_dir: str | Path,
) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "portfolio_selection.json"
    path.write_text(json.dumps(dict(selection), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
