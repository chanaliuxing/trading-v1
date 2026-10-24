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


def build_live_feedback(
    *,
    cycle_id: str,
    live_execution_report: Mapping[str, Any],
) -> Dict[str, Any]:
    rows: list[Dict[str, Any]] = []
    mapped_for_policy: list[Dict[str, Any]] = []
    for item in _as_list(_as_dict(live_execution_report).get("items")):
        row = _as_dict(item)
        candidate_id = str(row.get("candidate_id") or "")
        experiment_id = str(row.get("experiment_id") or "")
        fill_rate = float(row.get("fill_rate") or 0.0)
        slippage_bps = float(row.get("avg_slippage_bps") or 0.0)
        latency_ms = float(row.get("avg_latency_ms") or 0.0)
        risk_events = _as_list(row.get("risk_events"))
        realized_pnl = round((fill_rate * 120.0) - (slippage_bps * 3.0), 4)

        if row.get("execution_status") != "completed":
            next_action = "pause_candidate"
            feedback_status = "fill_quality_bad"
        elif slippage_bps > 7.0:
            next_action = "tighten_execution_constraints"
            feedback_status = "slippage_bad"
        elif fill_rate < 0.8:
            next_action = "demote_candidate"
            feedback_status = "fill_quality_bad"
        else:
            next_action = "keep_candidate"
            feedback_status = "execution_ok"

        live_row = {
            "candidate_id": candidate_id,
            "experiment_id": experiment_id,
            "realized_pnl": realized_pnl,
            "slippage_bps": slippage_bps,
            "fill_rate": fill_rate,
            "latency_ms": latency_ms,
            "risk_events": risk_events,
            "next_action": next_action,
            "feedback_status": feedback_status,
            "source_cycle": cycle_id,
        }
        rows.append(live_row)
        mapped_for_policy.append(
            {
                "candidate_id": candidate_id,
                "experiment_id": experiment_id,
                "feedback_status": feedback_status,
                "next_action": next_action,
                "slippage_delta": round(slippage_bps - 6.0, 4),
                "fill_quality": round(fill_rate - 0.9, 4),
                "latency_observed": latency_ms,
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": rows,
        "policy_mapped_feedback": mapped_for_policy,
    }


def persist_live_feedback(
    *,
    feedback: Mapping[str, Any],
    output_dir: str | Path,
    state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    feedback_path = out / "live_feedback.json"
    feedback_path.write_text(json.dumps(dict(feedback), indent=2, ensure_ascii=False), encoding="utf-8")

    state_dir = Path(state_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "live_feedback_index.json"
    payload = {"updated_at": None, "items": []}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {"updated_at": None, "items": []}

    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    for row in _as_list(_as_dict(feedback).get("items")):
        item = _as_dict(row)
        item["source"] = "live"
        items.append(item)
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = f"{_as_dict(item).get('candidate_id')}|{_as_dict(item).get('experiment_id')}|{_as_dict(item).get('source')}"
        dedup[key] = dict(item)
    payload = {"updated_at": _iso_now(), "items": list(dedup.values())}
    index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"live_feedback_json": str(feedback_path), "live_feedback_index_json": str(index_path)}
