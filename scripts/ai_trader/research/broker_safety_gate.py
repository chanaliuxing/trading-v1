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


def build_broker_safety_gate(
    *,
    cycle_id: str,
    broker_profile_report: Mapping[str, Any],
    operator_controls: Mapping[str, Any],
    preflight_check: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    health_monitor_report: Mapping[str, Any],
    kill_switch_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    profile = _as_dict(broker_profile_report)
    controls = _as_dict(operator_controls)
    preflight = _as_dict(preflight_check)
    admission = _as_dict(admission_gate)
    health = _as_dict(health_monitor_report)
    kill = _as_dict(kill_switch_report)

    is_real = str(profile.get("broker_profile") or "paper") == "real"
    checks = {
        "operator_live_enabled": bool(controls.get("live_enabled", False)),
        "preflight_ready": str(preflight.get("overall_status") or "") == "ready",
        "admission_pass": int(_as_dict(admission.get("summary")).get("approved") or 0) > 0,
        "no_critical_health": int(_as_dict(health.get("summary")).get("critical") or 0) == 0,
        "no_kill_switch_active": not bool(kill.get("kill_switch_active", False)),
        "broker_credential_valid": bool(profile.get("credential_valid", False)),
    }
    failed = [k for k, v in checks.items() if not v]

    if not is_real:
        gate_status = "not_applicable"
        executable_real = False
        reasons = ["profile_not_real"]
    elif failed:
        gate_status = "rejected"
        executable_real = False
        reasons = failed
    else:
        gate_status = "approved"
        executable_real = True
        reasons = ["all_checks_passed"]

    payload = {
        "cycle_id": cycle_id,
        "broker_profile": str(profile.get("broker_profile") or "paper"),
        "gate_status": gate_status,
        "executable_real": executable_real,
        "checks": checks,
        "reasons": reasons,
        "generated_at": _iso_now(),
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_safety_gate.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"broker_safety_gate_json": str(path), "broker_safety_gate": payload}

