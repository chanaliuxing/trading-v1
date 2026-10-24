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


def apply_live_batch_feedback(
    *,
    cycle_id: str,
    live_batch_plan: Mapping[str, Any],
    intraday_batch_report: Mapping[str, Any],
    eod_batch_close_report: Mapping[str, Any],
    candidate_lifecycle_index: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    launch_registry: Mapping[str, Any],
    state_root: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    intraday_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(intraday_batch_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    close_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(eod_batch_close_report).get("candidate_outcomes"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    selected = [str(_as_dict(x).get("candidate_id") or "") for x in _as_list(_as_dict(live_batch_plan).get("candidates"))]
    selected = [x for x in selected if x]
    demoted: list[str] = []
    promoted: list[str] = []
    paused: list[str] = []
    feedback_items: list[Dict[str, Any]] = []

    lifecycle = _as_dict(candidate_lifecycle_index)
    lifecycle_rows = [_as_dict(x) for x in _as_list(lifecycle.get("candidates"))]
    active = _as_dict(active_registry)
    active_rows = [_as_dict(x) for x in _as_list(active.get("items"))]
    launch = _as_dict(launch_registry)

    for candidate_id in selected:
        intra = _as_dict(intraday_by_id.get(candidate_id))
        close = _as_dict(close_by_id.get(candidate_id))
        actions = _as_list(intra.get("actions"))
        close_status = str(close.get("close_status") or "")
        if "flatten_one" in actions or "pause_one" in actions or close_status == "forced_flatten":
            outcome = "batch_mixed"
            next_action = "selective_demote"
            demoted.append(candidate_id)
            paused.append(candidate_id)
            mapped_status = "fill_quality_bad"
        elif "halt_batch" in actions:
            outcome = "batch_bad"
            next_action = "pause_batch_candidates"
            paused.append(candidate_id)
            mapped_status = "slippage_bad"
        else:
            outcome = "batch_ok"
            next_action = "keep_or_promote_subset"
            promoted.append(candidate_id)
            mapped_status = "execution_ok"

        feedback_items.append(
            {
                "candidate_id": candidate_id,
                "feedback_status": mapped_status,
                "next_action": (
                    "tighten_constraints"
                    if mapped_status in {"slippage_bad", "fill_quality_bad"}
                    else "keep_candidate"
                ),
                "source": "live_batch",
            }
        )

        for row in lifecycle_rows:
            if str(row.get("candidate_id") or "") != candidate_id:
                continue
            if candidate_id in demoted:
                row["current_state"] = "demoted"
                row["last_transition_reason"] = ["live_batch_selective_demote"]
            elif candidate_id in promoted:
                row["current_state"] = "paper_active"
                row["last_transition_reason"] = ["live_batch_keep_or_promote"]
            row["last_evaluated_at"] = _iso_now()

        for row in active_rows:
            if str(row.get("candidate_id") or "") != candidate_id:
                continue
            if candidate_id in paused:
                row["status"] = "halted"
                row["last_action"] = "live_batch_pause"
            else:
                row["status"] = "active"
                row["last_action"] = "live_batch_continue"
            row["last_run_at"] = _iso_now()

    lifecycle["candidates"] = lifecycle_rows
    lifecycle["updated_at"] = _iso_now()
    active["items"] = active_rows
    active["updated_at"] = _iso_now()
    launch["updated_at"] = _iso_now()
    if paused:
        launch["last_verdict"] = "hold_live"

    state_dir = Path(state_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "candidate_lifecycle_index.json").write_text(json.dumps(lifecycle, indent=2, ensure_ascii=False), encoding="utf-8")
    (state_dir / "active_registry.json").write_text(json.dumps(active, indent=2, ensure_ascii=False), encoding="utf-8")
    (state_dir / "launch_registry.json").write_text(json.dumps(launch, indent=2, ensure_ascii=False), encoding="utf-8")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "live_batch_feedback_report.json"
    payload = {
        "cycle_id": cycle_id,
        "batch_id": str(_as_dict(live_batch_plan).get("batch_id") or ""),
        "batch_outcome": (
            "batch_bad"
            if paused and not promoted
            else "batch_mixed"
            if paused and promoted
            else "batch_ok"
        ),
        "promoted_subset": promoted,
        "demoted_subset": demoted,
        "paused_candidates": paused,
        "feedback_items": feedback_items,
        "samples": {
            "batch_ok": "keep/promote subset",
            "batch_mixed": "selective demote",
            "batch_bad": "pause batch candidates",
            "batch_risk_event": "tighten constraints",
        },
        "selective_demote_sample": {
            "candidate_id": (demoted[0] if demoted else "sample-demote"),
            "action": "selective_demote",
        },
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"live_batch_feedback_report_json": str(path), "live_batch_feedback_report": payload}
