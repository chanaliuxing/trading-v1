from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha1
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


def _batch_id(cycle_id: str, mode: str) -> str:
    token = sha1(f"{cycle_id}|{mode}".encode("utf-8")).hexdigest()[:10]
    return f"batch-{cycle_id}-{mode}-{token}"


def build_launch_batch(
    *,
    cycle_id: str,
    mode: str,
    launch_verdict: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    allocation_plan: Mapping[str, Any],
    portfolio_selection: Mapping[str, Any],
    evidence_integrity_report: Mapping[str, Any],
    min_candidates: int = 2,
) -> Dict[str, Any]:
    mode_text = "small_live" if str(mode or "").strip().lower() == "small_live" else "paper"
    verdict_text = str(_as_dict(launch_verdict).get("verdict") or "hold_paper")
    evidence_blocked = str(_as_dict(evidence_integrity_report).get("overall_status") or "") != "passed"
    admission_rows = [_as_dict(x) for x in _as_list(_as_dict(admission_gate).get("items"))]
    allocation_rows = {
        str(_as_dict(x).get("experiment_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(allocation_plan).get("items"))
        if str(_as_dict(x).get("experiment_id") or "")
    }
    portfolio_rejected = {
        str(_as_dict(x).get("candidate_id") or _as_dict(x).get("experiment_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(portfolio_selection).get("rejected_candidates"))
        if str(_as_dict(x).get("candidate_id") or _as_dict(x).get("experiment_id") or "")
    }

    total_cap_limit = 200_000.0 if mode_text == "paper" else 60_000.0
    total_risk_limit = 300.0 if mode_text == "paper" else 120.0
    candidates: list[Dict[str, Any]] = []
    rejected: list[Dict[str, Any]] = []
    used_capital = 0.0
    used_risk = 0.0

    for row in admission_rows:
        cid = str(row.get("candidate_id") or "")
        exp_id = str(row.get("experiment_id") or cid)
        if not cid:
            continue
        alloc = _as_dict(allocation_rows.get(exp_id))
        req_cap = float(alloc.get("max_capital") or 25_000.0)
        req_risk = float(alloc.get("risk_budget_bps") or 30.0)
        reasons: list[str] = []
        if str(row.get("admission_status") or "") != "approved":
            reasons.append("admission_rejected")
        if cid in portfolio_rejected:
            reason = str(_as_dict(portfolio_rejected[cid]).get("reason") or "portfolio_rejected")
            reasons.append(reason)
        if evidence_blocked:
            reasons.append("evidence_incomplete")
        if used_capital + req_cap > total_cap_limit or used_risk + req_risk > total_risk_limit:
            reasons.append("risk_budget_exceeded")
        if reasons:
            rejected.append(
                {
                    "candidate_id": cid,
                    "experiment_id": exp_id,
                    "reasons": sorted(set(reasons)),
                }
            )
            continue
        used_capital += req_cap
        used_risk += req_risk
        candidates.append(
            {
                "candidate_id": cid,
                "experiment_id": exp_id,
                "target_weight": float(alloc.get("target_weight") or 0.0),
                "max_capital": req_cap,
                "risk_budget_bps": req_risk,
                "evidence_refs": {
                    "admission_gate": "admission_gate.json",
                    "launch_verdict": "launch_verdict.json",
                    "evidence_integrity": "evidence_integrity_report.json",
                },
            }
        )

    if verdict_text not in {"go_paper", "go_small_live"}:
        status = "blocked"
    elif len(candidates) < min_candidates:
        status = "degraded"
    else:
        status = "ready"

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "batch_id": _batch_id(cycle_id, mode_text),
        "mode": mode_text,
        "candidates": candidates,
        "rejected_candidates": rejected,
        "total_capital": round(used_capital, 2),
        "risk_budget": round(used_risk, 2),
        "status": status,
        "verdict_ref": "launch_verdict.json",
    }

