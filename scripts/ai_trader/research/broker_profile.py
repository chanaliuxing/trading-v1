from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.operator_controls import load_operator_controls


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"
ALLOWED_BROKER_PROFILES = {"paper", "mock", "real"}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_broker_launch_switch() -> Dict[str, Any]:
    return {
        "paper_only": True,
        "real_broker_enabled": False,
        "real_broker_dry_run_only": True,
        "updated_at": _iso_now(),
    }


def load_broker_launch_switch(
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    create_if_missing: bool = True,
) -> Dict[str, Any]:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "broker_launch_switch.json"
    if not path.exists():
        payload = default_broker_launch_switch()
        if create_if_missing:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        payload = default_broker_launch_switch()
        payload.update(_as_dict(obj))
        payload["updated_at"] = payload.get("updated_at") or _iso_now()
        return payload
    except Exception:
        payload = default_broker_launch_switch()
        if create_if_missing:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload


def persist_broker_launch_switch(
    launch_switch: Mapping[str, Any],
    *,
    output_dir: str | Path,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    payload = default_broker_launch_switch()
    payload.update(_as_dict(launch_switch))
    payload["updated_at"] = _iso_now()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    output_path = out / "broker_launch_switch.json"
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "broker_launch_switch.json"
    state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(output_path)


def _resolve_real_broker_credentials() -> Dict[str, Any]:
    api_key = str(os.getenv("REAL_BROKER_API_KEY", "")).strip()
    api_secret = str(os.getenv("REAL_BROKER_API_SECRET", "")).strip()
    if api_key and api_secret:
        return {"credential_source": "env", "credential_valid": True}

    file_path = str(os.getenv("REAL_BROKER_CREDENTIALS_FILE", "")).strip()
    if file_path:
        p = Path(file_path)
        if p.exists():
            try:
                payload = _as_dict(json.loads(p.read_text(encoding="utf-8")))
                if str(payload.get("api_key") or "").strip() and str(payload.get("api_secret") or "").strip():
                    return {"credential_source": f"file:{p}", "credential_valid": True}
            except Exception:
                pass
        return {"credential_source": f"file:{p}", "credential_valid": False}

    return {"credential_source": "missing", "credential_valid": False}


def build_broker_profile_report(
    *,
    cycle_id: str,
    requested_profile: str,
    output_dir: str | Path,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> Dict[str, Any]:
    operator_controls = load_operator_controls(state_root=state_root, create_if_missing=True)
    launch_switch = load_broker_launch_switch(state_root=state_root, create_if_missing=True)
    requested = str(requested_profile or "paper").strip().lower()
    if requested not in ALLOWED_BROKER_PROFILES:
        requested = "paper"

    resolved_profile = requested
    reasons: list[str] = []
    if bool(launch_switch.get("paper_only", True)):
        resolved_profile = "paper"
        reasons.append("paper_only_enabled")
    elif requested == "real" and not bool(launch_switch.get("real_broker_enabled", False)):
        resolved_profile = "paper"
        reasons.append("real_broker_disabled")
    elif requested == "real" and not bool(operator_controls.get("live_enabled", False)):
        resolved_profile = "paper"
        reasons.append("operator_live_disabled")

    real_creds = _resolve_real_broker_credentials()
    credential_source = (
        real_creds["credential_source"] if resolved_profile == "real" else f"{resolved_profile}_builtin"
    )
    credential_valid = bool(real_creds["credential_valid"]) if resolved_profile == "real" else True
    dry_run_only = bool(launch_switch.get("real_broker_dry_run_only", True))

    if resolved_profile == "real":
        account_mode = "live_dry_run" if dry_run_only else "live_execution_blocked"
        enabled = bool(launch_switch.get("real_broker_enabled", False))
        safe_default = bool(dry_run_only)
    elif resolved_profile == "mock":
        account_mode = "paper_sim"
        enabled = True
        safe_default = True
    else:
        account_mode = "paper"
        enabled = True
        safe_default = True

    payload = {
        "cycle_id": cycle_id,
        "requested_profile": requested_profile,
        "broker_profile": resolved_profile,
        "enabled": enabled,
        "credential_source": credential_source,
        "credential_valid": credential_valid,
        "account_mode": account_mode,
        "safe_default": safe_default,
        "launch_switch": {
            "paper_only": bool(launch_switch.get("paper_only", True)),
            "real_broker_enabled": bool(launch_switch.get("real_broker_enabled", False)),
            "real_broker_dry_run_only": bool(launch_switch.get("real_broker_dry_run_only", True)),
        },
        "reasons": reasons or ["profile_resolved"],
        "generated_at": _iso_now(),
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_profile_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"broker_profile_report_json": str(path), "broker_profile_report": payload, "launch_switch": launch_switch}

