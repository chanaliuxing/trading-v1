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


def _hhmm_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")


def _within_window(hhmm: str, window: str) -> bool:
    try:
        start, end = [x.strip() for x in str(window).split("-", 1)]
        return start <= hhmm <= end
    except Exception:
        return False


def evaluate_session_control(
    *,
    cycle_id: str,
    live_candidates_dir: str | Path,
    operator_controls: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
    simulate_pre_close_flatten: bool = False,
) -> Dict[str, Any]:
    controls = _as_dict(operator_controls)
    global_pause = bool(controls.get("global_pause"))
    no_trade_windows = [str(x) for x in _as_list(controls.get("no_trade_windows"))]
    cooldown_candidates = {str(x) for x in _as_list(controls.get("cooldown_candidates"))}
    blocked_candidates = {str(x) for x in _as_list(controls.get("blocked_candidates"))}
    force_flatten = bool(controls.get("force_flatten"))
    hhmm = _hhmm_now()
    in_no_trade = any(_within_window(hhmm, w) for w in no_trade_windows)
    guard = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }

    src = Path(live_candidates_dir)
    items: list[Dict[str, Any]] = []
    for candidate_dir in sorted(src.iterdir()) if src.exists() else []:
        if not candidate_dir.is_dir():
            continue
        manifest_path = candidate_dir / "live_manifest.json"
        if not manifest_path.exists():
            continue
        m = _as_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        cid = str(m.get("candidate_id") or candidate_dir.name)
        exp_id = str(m.get("experiment_id") or "")
        reasons: list[str] = []
        allowed = True
        flatten = False

        if global_pause:
            allowed = False
            reasons.append("global_pause")
        if cid in blocked_candidates:
            allowed = False
            reasons.append("blocked_candidate")
        if in_no_trade:
            allowed = False
            reasons.append("no_trade_window")
        if cid in cooldown_candidates:
            allowed = False
            reasons.append("cooldown_after_kill_switch")
        if _as_dict(guard.get(cid)).get("kill_switch"):
            allowed = False
            reasons.append("kill_switch_active")
        if force_flatten or simulate_pre_close_flatten:
            flatten = True
            reasons.append("pre_close_flatten")

        items.append(
            {
                "candidate_id": cid,
                "experiment_id": exp_id,
                "session_allowed": allowed,
                "flatten_required": flatten,
                "reasons": reasons or ["session_open"],
                "evaluated_at": _iso_now(),
            }
        )
    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": items,
        "summary": {
            "allowed": sum(1 for x in items if x.get("session_allowed")),
            "blocked": sum(1 for x in items if not x.get("session_allowed")),
            "flatten": sum(1 for x in items if x.get("flatten_required")),
        },
    }
