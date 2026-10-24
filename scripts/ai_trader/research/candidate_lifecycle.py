from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"


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


def load_candidate_lifecycle_index(*, state_root: str | Path = DEFAULT_STATE_ROOT) -> Dict[str, Any]:
    root = Path(state_root)
    path = root / "candidate_lifecycle_index.json"
    if not path.exists():
        return {"updated_at": None, "candidates": []}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            if not isinstance(obj.get("candidates"), list):
                obj["candidates"] = []
            return obj
    except Exception:
        pass
    return {"updated_at": None, "candidates": []}


def persist_candidate_lifecycle_index(
    *,
    index: Mapping[str, Any],
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "candidate_lifecycle_index.json"
    payload = dict(index)
    payload["updated_at"] = _iso_now()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def apply_candidate_lifecycle(
    *,
    cycle_id: str,
    lifecycle_index: Mapping[str, Any],
    candidate_gate_report: Mapping[str, Any],
    candidate_retention_report: Mapping[str, Any],
) -> Dict[str, Any]:
    now = _iso_now()
    existing = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(lifecycle_index).get("candidates"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    retention = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(candidate_retention_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    decisions = [_as_dict(x) for x in _as_list(_as_dict(candidate_gate_report).get("items"))]
    updated_rows: list[Dict[str, Any]] = []
    transitions: list[Dict[str, Any]] = []

    for decision in decisions:
        candidate_id = str(decision.get("candidate_id") or "")
        if not candidate_id:
            continue
        prev = _as_dict(existing.get(candidate_id))
        old_state = str(prev.get("current_state") or "research_only")
        new_state = str(decision.get("next_state") or old_state or "research_only")
        changed = bool(decision.get("changed")) or (new_state != old_state)
        history = _as_list(prev.get("history"))
        if changed:
            history.append(
                {
                    "at": now,
                    "cycle_id": cycle_id,
                    "from": old_state,
                    "to": new_state,
                    "reason": _as_list(decision.get("reasons")),
                    "source_feedback": decision.get("source_feedback"),
                }
            )
            transitions.append(
                {
                    "candidate_id": candidate_id,
                    "experiment_id": decision.get("experiment_id"),
                    "from": old_state,
                    "to": new_state,
                    "reason": _as_list(decision.get("reasons")),
                    "source_cycle": decision.get("source_cycle"),
                    "source_candidate": decision.get("source_candidate"),
                    "source_feedback": decision.get("source_feedback"),
                }
            )
        row = {
            "candidate_id": candidate_id,
            "experiment_id": str(decision.get("experiment_id") or prev.get("experiment_id") or ""),
            "current_state": new_state,
            "state_entered_at": now if changed else str(prev.get("state_entered_at") or now),
            "last_evaluated_at": now,
            "last_transition_reason": _as_list(decision.get("reasons")),
            "last_source_cycle": cycle_id,
            "last_source_feedback": decision.get("source_feedback"),
            "stale_cycles": int(decision.get("stale_cycles") or 0),
            "market": str(decision.get("market") or prev.get("market") or "").strip().lower(),
            "symbol": str(decision.get("symbol") or prev.get("symbol") or "").strip().upper(),
            "strategy_family": str(decision.get("strategy_family") or prev.get("strategy_family") or ""),
            "retention": _as_dict(retention.get(candidate_id)) or _as_dict(prev.get("retention")),
            "history": history[-20:],
        }
        updated_rows.append(row)

    for candidate_id, prev in existing.items():
        if candidate_id in {str(_as_dict(x).get("candidate_id") or "") for x in updated_rows}:
            continue
        row = dict(prev)
        row["last_evaluated_at"] = now
        updated_rows.append(row)

    updated_index = {
        "updated_at": now,
        "candidates": sorted(updated_rows, key=lambda x: str(x.get("candidate_id") or "")),
    }
    return {
        "index": updated_index,
        "report": {
            "cycle_id": cycle_id,
            "generated_at": now,
            "transitions": transitions,
            "summary": {
                "candidate_count": len(updated_rows),
                "transition_count": len(transitions),
                "state_counts": {
                    "research_only": sum(1 for x in updated_rows if x.get("current_state") == "research_only"),
                    "shadow_candidate": sum(1 for x in updated_rows if x.get("current_state") == "shadow_candidate"),
                    "shadow_active": sum(1 for x in updated_rows if x.get("current_state") == "shadow_active"),
                    "paper_candidate": sum(1 for x in updated_rows if x.get("current_state") == "paper_candidate"),
                    "paper_active": sum(1 for x in updated_rows if x.get("current_state") == "paper_active"),
                    "demoted": sum(1 for x in updated_rows if x.get("current_state") == "demoted"),
                    "retired": sum(1 for x in updated_rows if x.get("current_state") == "retired"),
                },
            },
        },
    }
