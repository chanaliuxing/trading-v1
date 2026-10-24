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


def build_allocation_plan(
    *,
    portfolio_selection: Mapping[str, Any],
    risk_budget_bps_total: float = 100.0,
    capital_total: float = 100_000.0,
) -> Dict[str, Any]:
    selection = _as_dict(portfolio_selection)
    raw = [_as_dict(x) for x in _as_list(selection.get("allocation_plan"))]
    if not raw:
        selected = [str(x) for x in _as_list(selection.get("selected_candidates")) if str(x)]
        if selected:
            even = round(1.0 / len(selected), 4)
            raw = [
                {
                    "experiment_id": exp_id,
                    "target_weight": even,
                    "max_capital": round(capital_total * even, 2),
                    "risk_budget_bps": round(risk_budget_bps_total * even, 2),
                    "allocation_reason": "fallback_equal_weight",
                }
                for exp_id in selected
            ]

    # Normalize to keep totals bounded.
    total_weight = sum(float(_as_dict(x).get("target_weight") or 0.0) for x in raw) or 1.0
    items: list[Dict[str, Any]] = []
    for row in raw:
        exp_id = str(row.get("experiment_id") or "")
        weight = max(0.0, float(row.get("target_weight") or 0.0) / total_weight)
        items.append(
            {
                "experiment_id": exp_id,
                "target_weight": round(weight, 4),
                "max_capital": round(capital_total * weight, 2),
                "risk_budget_bps": round(risk_budget_bps_total * weight, 2),
                "allocation_reason": str(row.get("allocation_reason") or "normalized_from_selection"),
            }
        )
    return {"items": items, "totals": {"target_weight": round(sum(x["target_weight"] for x in items), 4), "risk_budget_bps": round(sum(x["risk_budget_bps"] for x in items), 2)}}


def persist_allocation_plan(plan: Mapping[str, Any], *, output_dir: str | Path) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "allocation_plan.json"
    path.write_text(json.dumps(dict(plan), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def build_shadow_candidates(
    *,
    promotion_gate: Mapping[str, Any],
    allocation_plan: Mapping[str, Any],
    candidate_results: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir) / "shadow_candidates"
    out.mkdir(parents=True, exist_ok=True)
    allocation_by_exp = {str(_as_dict(x).get("experiment_id") or ""): _as_dict(x) for x in _as_list(_as_dict(allocation_plan).get("items"))}
    result_by_exp = {str(_as_dict(x).get("experiment_id") or ""): _as_dict(x) for x in candidate_results}

    created: list[str] = []
    for row in _as_list(_as_dict(promotion_gate).get("items")):
        gate_row = _as_dict(row)
        exp_id = str(gate_row.get("experiment_id") or "")
        if gate_row.get("gate_status") != "passed":
            continue
        target = str(gate_row.get("promotion_target") or "")
        if target not in {"paper_candidate", "shadow_candidate"}:
            continue
        cdir = out / exp_id
        cdir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "experiment_id": exp_id,
            "promotion_target": target,
            "bundle_status": "ready",
            "gate_reasons": gate_row.get("gate_reasons") or [],
        }
        experiment_ref = {"experiment_id": exp_id, "result": result_by_exp.get(exp_id, {})}
        allocation_ref = allocation_by_exp.get(exp_id, {"experiment_id": exp_id})
        execution_constraints = {
            "mode": "shadow" if target == "shadow_candidate" else "paper",
            "max_orders_per_day": 20,
            "max_notional": allocation_ref.get("max_capital"),
            "risk_budget_bps": allocation_ref.get("risk_budget_bps"),
        }
        (cdir / "candidate_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (cdir / "experiment_ref.json").write_text(json.dumps(experiment_ref, indent=2, ensure_ascii=False), encoding="utf-8")
        (cdir / "allocation_ref.json").write_text(json.dumps(allocation_ref, indent=2, ensure_ascii=False), encoding="utf-8")
        (cdir / "execution_constraints.json").write_text(json.dumps(execution_constraints, indent=2, ensure_ascii=False), encoding="utf-8")
        created.append(str(cdir))
    return {"shadow_candidates_dir": str(out), "candidate_dirs": created}

