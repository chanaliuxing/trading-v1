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


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_manual_approval(
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    create_if_missing: bool = True,
) -> Dict[str, Any]:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "manual_approval.json"
    if not path.exists():
        payload = {"approvals": [], "updated_at": _iso_now()}
        if create_if_missing:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        payload = _as_dict(obj)
        approvals = [_as_dict(x) for x in _as_list(payload.get("approvals"))]
        payload["approvals"] = approvals
        payload["updated_at"] = str(payload.get("updated_at") or _iso_now())
        return payload
    except Exception:
        payload = {"approvals": [], "updated_at": _iso_now()}
        if create_if_missing:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload


def persist_manual_approval(
    payload: Mapping[str, Any],
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "manual_approval.json"
    body = _as_dict(payload)
    body["updated_at"] = _iso_now()
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def build_manual_approval_report(
    *,
    cycle_id: str,
    candidate_id: str,
    output_dir: str | Path,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> Dict[str, Any]:
    approvals_doc = load_manual_approval(state_root=state_root, create_if_missing=True)
    approvals = [_as_dict(x) for x in _as_list(approvals_doc.get("approvals"))]
    row = {}
    for item in approvals:
        if str(item.get("candidate_id") or "") == str(candidate_id):
            row = item
            break
    now = datetime.now(timezone.utc)
    approved = bool(row.get("approved", False))
    expires_at = _parse_dt(row.get("expires_at"))
    expired = bool(expires_at and expires_at < now)
    if not row:
        status = "rejected"
        reasons = ["approval_missing"]
    elif expired:
        status = "rejected"
        reasons = ["approval_expired"]
    elif not approved:
        status = "rejected"
        reasons = ["approval_not_granted"]
    else:
        status = "approved"
        reasons = ["approval_granted"]

    approved_sample = {
        "candidate_id": str(candidate_id),
        "approved": True,
        "approved_by": str(row.get("approved_by") or "operator"),
        "approved_at": str(row.get("approved_at") or _iso_now()),
        "max_live_capital": float(row.get("max_live_capital") or 1000.0),
        "expires_at": str(row.get("expires_at") or (now.replace(hour=23, minute=59, second=0, microsecond=0).isoformat())),
        "notes": str(row.get("notes") or "approved_sample"),
    }
    rejected_sample = {
        "candidate_id": f"{candidate_id}-expired",
        "approved": False,
        "approved_by": "operator",
        "approved_at": _iso_now(),
        "max_live_capital": 500.0,
        "expires_at": (now.replace(hour=0, minute=0, second=0, microsecond=0)).isoformat(),
        "notes": "expired_sample",
    }

    payload = {
        "cycle_id": cycle_id,
        "candidate_id": str(candidate_id),
        "status": status,
        "reasons": reasons,
        "manual_approval": row or {},
        "approved_sample": approved_sample,
        "rejected_sample": rejected_sample,
        "generated_at": _iso_now(),
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manual_approval_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"manual_approval_report_json": str(path), "manual_approval_report": payload}

