from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_broker_launch_barrier(
    *,
    cycle_id: str,
    broker_readonly_probe: Mapping[str, Any],
    broker_reconcile_report: Mapping[str, Any],
    eod_closeout_report: Mapping[str, Any],
    launch_registry: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    system_status: Mapping[str, Any],
    state_root: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    probe = _as_dict(broker_readonly_probe)
    reconcile = _as_dict(broker_reconcile_report)
    eod = _as_dict(eod_closeout_report)
    lreg = _as_dict(launch_registry)
    areg = _as_dict(active_registry)
    sstat = _as_dict(system_status)
    block_reasons = []
    if str(probe.get("status") or "") != "success" and str(probe.get("status") or "") != "skipped":
        block_reasons.append("readonly_probe_not_success")
    summary = _as_dict(reconcile.get("summary"))
    if any(int(summary.get(k) or 0) > 0 for k in ("missing_order", "status_mismatch", "fill_mismatch", "position_mismatch")):
        block_reasons.append("reconcile_has_mismatch")
    if not bool(eod.get("closeout_completed", False)):
        block_reasons.append("eod_closeout_incomplete")

    blocked = bool(block_reasons)
    barrier_status = "blocked" if blocked else "passed"
    if blocked:
        sstat["live_enabled"] = False
        sstat["kill_switch_active"] = True
        sstat["barrier_blocked"] = True
        lreg["last_verdict"] = "hold_live"
        items = []
        for row in (areg.get("items") or []):
            item = dict(row) if isinstance(row, dict) else {}
            if item.get("status") == "active":
                item["status"] = "inactive"
                item["last_action"] = "barrier_block"
            items.append(item)
        areg["items"] = items
    else:
        sstat["barrier_blocked"] = False

    sstat["cycle_id"] = cycle_id
    sstat["updated_at"] = _iso_now()
    lreg["cycle_id"] = cycle_id
    lreg["updated_at"] = _iso_now()
    areg["updated_at"] = _iso_now()

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_launch_barrier.json"
    payload = {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "barrier_status": barrier_status,
        "block_reasons": block_reasons,
        "actions": ["pause_next_day_active"] if blocked else ["allow_next_day_active"],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "launch_registry.json").write_text(json.dumps(lreg, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "active_registry.json").write_text(json.dumps(areg, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "system_status.json").write_text(json.dumps(sstat, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"broker_launch_barrier_json": str(path), "broker_launch_barrier": payload}

