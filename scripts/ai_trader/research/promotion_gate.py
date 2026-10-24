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


def build_promotion_gate(
    *,
    candidate_results: Sequence[Mapping[str, Any]],
    portfolio_selection: Mapping[str, Any],
) -> Dict[str, Any]:
    selected_order = [str(x) for x in _as_list(_as_dict(portfolio_selection).get("selected_candidates")) if str(x)]
    selected = set(selected_order)
    rows: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    for result in candidate_results:
        row = _as_dict(result)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        status = str(row.get("status") or "").strip().lower()
        robustness = _as_dict(row.get("robustness"))
        reasons: list[str] = []
        gate_status = "rejected"
        target = "research_only"

        if status in {"blocked", "deferred", "failed", "error"}:
            reasons.append(f"status_{status}")
        if not robustness.get("ok", False):
            reasons.append("robustness_not_passed")
        if exp_id not in selected:
            reasons.append("not_selected_by_portfolio")

        if not reasons:
            gate_status = "passed"
            target = "paper_candidate"
        # first selected candidate can go shadow if still passed
        if gate_status == "passed":
            first_selected = selected_order[0] if selected_order else ""
            if exp_id == first_selected:
                target = "shadow_candidate"

        rows.append(
            {
                "experiment_id": exp_id,
                "gate_status": gate_status,
                "promotion_target": target if gate_status == "passed" else "research_only",
                "gate_reasons": reasons or ["passed_all_gates"],
            }
        )
        seen_ids.add(exp_id)

    # Selected candidates from historical pool remain research_only if current-cycle result is absent.
    # Promotion requires explicit in-cycle robustness/status evidence.
    for exp_id in selected_order:
        if exp_id in seen_ids:
            continue
        rows.append(
            {
                "experiment_id": exp_id,
                "gate_status": "rejected",
                "promotion_target": "research_only",
                "gate_reasons": ["missing_cycle_result", "robustness_not_passed"],
            }
        )

    return {"items": rows}


def persist_promotion_gate(gate: Mapping[str, Any], *, output_dir: str | Path) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "promotion_gate.json"
    path.write_text(json.dumps(dict(gate), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
