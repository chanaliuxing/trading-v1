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


def load_active_registry(*, state_root: str | Path = DEFAULT_STATE_ROOT) -> Dict[str, Any]:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "active_registry.json"
    if not path.exists():
        return {"updated_at": None, "items": []}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            if not isinstance(obj.get("items"), list):
                obj["items"] = []
            return obj
    except Exception:
        pass
    return {"updated_at": None, "items": []}


def persist_active_registry(
    *,
    registry: Mapping[str, Any],
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "active_registry.json"
    payload = dict(registry)
    payload["updated_at"] = _iso_now()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def update_active_registry(
    *,
    cycle_id: str,
    existing_registry: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    health_monitor_report: Mapping[str, Any],
    auto_halt_report: Mapping[str, Any],
) -> Dict[str, Any]:
    existing = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(existing_registry).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    admission = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(admission_gate).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    health = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(health_monitor_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    halt = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(auto_halt_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "") and str(_as_dict(x).get("candidate_id")) != "*"
    }

    ids = sorted(set(list(existing.keys()) + list(admission.keys()) + list(health.keys()) + list(halt.keys())))
    updated: list[Dict[str, Any]] = []
    transitions: list[Dict[str, Any]] = []
    for cid in ids:
        prev = _as_dict(existing.get(cid))
        adm = _as_dict(admission.get(cid))
        hl = _as_dict(health.get(cid))
        ah = _as_dict(halt.get(cid))
        prev_status = str(prev.get("status") or "inactive")
        new_status = "active" if str(adm.get("admission_status") or "rejected") == "approved" else "inactive"
        if ah:
            action = str(ah.get("action") or "")
            if action in {"pause_candidate", "flatten_candidate", "demote_candidate"}:
                new_status = "halted"
        row = {
            "candidate_id": cid,
            "mode": str(adm.get("mode") or prev.get("mode") or "paper"),
            "status": new_status,
            "admission_status": str(adm.get("admission_status") or prev.get("admission_status") or "rejected"),
            "health_status": str(hl.get("health_status") or prev.get("health_status") or "unknown"),
            "last_run_at": _iso_now(),
            "last_action": str(ah.get("action") or ("admission_" + str(adm.get("admission_status") or "unknown"))),
            "experiment_id": str(adm.get("experiment_id") or hl.get("experiment_id") or prev.get("experiment_id") or ""),
            "last_cycle_id": cycle_id,
        }
        if row["status"] != prev_status:
            transitions.append(
                {
                    "candidate_id": cid,
                    "from": prev_status,
                    "to": row["status"],
                    "reason": row["last_action"],
                }
            )
        updated.append(row)

    return {
        "registry": {"updated_at": _iso_now(), "items": updated},
        "report": {
            "cycle_id": cycle_id,
            "generated_at": _iso_now(),
            "transitions": transitions,
            "summary": {
                "active": sum(1 for x in updated if x.get("status") == "active"),
                "inactive": sum(1 for x in updated if x.get("status") == "inactive"),
                "halted": sum(1 for x in updated if x.get("status") == "halted"),
            },
        },
    }
