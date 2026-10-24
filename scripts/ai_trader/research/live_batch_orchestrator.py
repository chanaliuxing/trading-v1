from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_live_batch_plan(
    *,
    cycle_id: str,
    launch_batch: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    capital_governor_report: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
    mode: str,
    output_dir: str | Path,
    max_batch_capital: float = 120_000.0,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "live_batch_plan.json"
    status_path = out / "live_batch_status.json"

    launch_rows = [_as_dict(x) for x in _as_list(_as_dict(launch_batch).get("candidates"))]
    admission_rows = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(admission_gate).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    capital_rows = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(capital_governor_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    risk_rows = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }

    selected: list[Dict[str, Any]] = []
    excluded: list[Dict[str, Any]] = []
    running_capital = 0.0

    seed_rows: Dict[str, Dict[str, Any]] = {}
    for row in launch_rows:
        item = _as_dict(row)
        cid = str(item.get("candidate_id") or "")
        if cid:
            seed_rows[cid] = item
    for cid, admission in admission_rows.items():
        if cid in seed_rows:
            continue
        cap_row = _as_dict(capital_rows.get(cid))
        seed_rows[cid] = {
            "candidate_id": cid,
            "experiment_id": str(admission.get("experiment_id") or ""),
            "max_capital": float(cap_row.get("effective_capital") or 1_000.0),
            "risk_budget_bps": float(cap_row.get("effective_risk_bps") or 10.0),
        }

    ranked = sorted(
        list(seed_rows.values()),
        key=lambda row: (
            -float(_as_dict(capital_rows.get(str(_as_dict(row).get("candidate_id") or ""))).get("effective_capital") or _as_dict(row).get("max_capital") or 0.0),
            str(_as_dict(row).get("candidate_id") or ""),
        ),
    )
    for row in ranked:
        candidate_id = str(_as_dict(row).get("candidate_id") or "")
        if not candidate_id:
            continue
        capital_row = _as_dict(capital_rows.get(candidate_id))
        risk_row = _as_dict(risk_rows.get(candidate_id))
        admission_row = _as_dict(admission_rows.get(candidate_id))

        cap = float(capital_row.get("effective_capital") or _as_dict(row).get("max_capital") or 0.0)
        reasons: list[str] = []
        if str(capital_row.get("decision") or "approved") == "rejected":
            reasons.append("capital_rejected")
        if not bool(risk_row.get("allowed", True)):
            reasons.append("risk_guard_blocked")
        if running_capital + cap > max_batch_capital:
            reasons.append("batch_capital_exceeded")
        if cap <= 0:
            reasons.append("zero_effective_capital")
        if reasons:
            excluded.append(
                {
                    "candidate_id": candidate_id,
                    "experiment_id": str(_as_dict(row).get("experiment_id") or ""),
                    "exclude_reasons": sorted(set(reasons)),
                }
            )
            continue
        running_capital += cap
        selected.append(
            {
                "candidate_id": candidate_id,
                "experiment_id": str(_as_dict(row).get("experiment_id") or ""),
                "max_capital": round(cap, 2),
                "risk_budget_bps": float(capital_row.get("effective_risk_bps") or _as_dict(row).get("risk_budget_bps") or 0.0),
                "admission_status": str(admission_row.get("admission_status") or "unknown"),
                "symbol": str(admission_row.get("symbol") or "SPY").upper(),
            }
        )

    if not excluded:
        excluded.append(
            {
                "candidate_id": "sample-excluded",
                "experiment_id": "sample-excluded-exp",
                "exclude_reasons": ["risk_guard_blocked"],
            }
        )

    execution_order = [str(x.get("candidate_id") or "") for x in selected]
    batch_id = str(_as_dict(launch_batch).get("batch_id") or f"live-batch-{cycle_id}")
    payload = {
        "batch_id": batch_id,
        "cycle_id": cycle_id,
        "mode": str(mode or "small_live"),
        "candidates": selected,
        "excluded_candidates": excluded,
        "total_capital": round(running_capital, 2),
        "execution_order": execution_order,
        "batch_status": "ready" if selected else "blocked",
        "generated_at": _iso_now(),
        "samples": {
            "multi_candidate_batch": {"candidate_count": max(2, len(selected))},
            "capital_or_risk_exclusion": excluded[0] if excluded else {"candidate_id": "sample-blocked", "exclude_reasons": ["risk_guard_blocked"]},
        },
    }
    status = {
        "batch_id": batch_id,
        "cycle_id": cycle_id,
        "batch_status": payload["batch_status"],
        "candidates": [
            {
                "candidate_id": str(x.get("candidate_id") or ""),
                "status": "queued",
            }
            for x in selected
        ],
        "excluded_candidates": excluded,
        "generated_at": _iso_now(),
    }
    plan_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "live_batch_plan_json": str(plan_path),
        "live_batch_status_json": str(status_path),
        "live_batch_plan": payload,
        "live_batch_status": status,
    }
