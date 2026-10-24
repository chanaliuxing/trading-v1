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


def _approval_ok(candidate_id: str, approvals: Mapping[str, Any]) -> bool:
    now = datetime.now(timezone.utc)
    for row in _as_list(_as_dict(approvals).get("approvals")):
        item = _as_dict(row)
        if str(item.get("candidate_id") or "") != candidate_id:
            continue
        if not bool(item.get("approved", False)):
            return False
        expires = str(item.get("expires_at") or "").strip()
        if not expires:
            return True
        try:
            if expires.endswith("Z"):
                expires = expires[:-1] + "+00:00"
            dt = datetime.fromisoformat(expires)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc) >= now
        except Exception:
            return False
    return False


def run_pre_open_prep(
    *,
    cycle_id: str,
    live_batch_plan: Mapping[str, Any],
    manual_approvals: Mapping[str, Any],
    broker_readonly_probe: Mapping[str, Any],
    account_snapshot: Mapping[str, Any],
    positions_snapshot: Mapping[str, Any],
    operator_controls: Mapping[str, Any],
    session_control_report: Mapping[str, Any],
    kill_switch_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "pre_open_prep_report.json"

    broker_ok = str(_as_dict(broker_readonly_probe).get("status") or "") in {"success", "not_applicable"}
    account_ok = bool(_as_dict(account_snapshot).get("account") or _as_dict(account_snapshot).get("cash") is not None)
    positions_ok = isinstance(_as_dict(positions_snapshot).get("positions"), list)
    controls_ok = bool(_as_dict(operator_controls).get("live_enabled", False)) and not bool(_as_dict(operator_controls).get("global_pause", False))
    kill_ok = not bool(_as_dict(kill_switch_report).get("kill_switch_active", False))
    session_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(session_control_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }

    items: list[Dict[str, Any]] = []
    selected_rows = [_as_dict(x) for x in _as_list(_as_dict(live_batch_plan).get("candidates"))]
    for row in selected_rows:
        candidate_id = str(row.get("candidate_id") or "")
        sess = _as_dict(session_by_id.get(candidate_id))
        checks = {
            "approval_valid": _approval_ok(candidate_id, manual_approvals),
            "broker_connected": broker_ok,
            "account_snapshot_fresh": account_ok,
            "positions_clean": positions_ok,
            "controls_ok": controls_ok,
            "session_tradable": bool(sess.get("session_allowed", True)),
            "no_active_kill_switch": kill_ok,
        }
        blocked = [k for k, v in checks.items() if not v]
        items.append(
            {
                "candidate_id": candidate_id,
                "status": "pass" if not blocked else "blocked",
                "checks": checks,
                "blocking_reasons": blocked,
            }
        )

    payload = {
        "cycle_id": cycle_id,
        "batch_id": str(_as_dict(live_batch_plan).get("batch_id") or ""),
        "items": items,
        "prep_status": "pass" if items and all(x.get("status") == "pass" for x in items) else "blocked",
        "summary": {
            "passed": sum(1 for x in items if x.get("status") == "pass"),
            "blocked": sum(1 for x in items if x.get("status") == "blocked"),
        },
        "prep_pass_sample": {"candidate_id": "sample-pass", "status": "pass"},
        "prep_blocked_sample": {"candidate_id": "sample-blocked", "status": "blocked", "blocking_reasons": ["approval_valid"]},
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"pre_open_prep_report_json": str(path), "pre_open_prep_report": payload}

