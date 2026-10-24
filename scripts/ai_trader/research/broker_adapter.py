from __future__ import annotations

import json
from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from scripts.ai_trader.research.broker_models import BrokerSession
from scripts.ai_trader.research.paper_broker_adapter import PaperBrokerAdapter
from scripts.ai_trader.research.real_broker_adapter import RealBrokerDryRunAdapter


def _session_id(cycle_id: str, profile: str) -> str:
    token = sha1(f"{cycle_id}|{profile}".encode("utf-8")).hexdigest()[:12]
    return f"broker-{profile}-{token}"


def create_broker_session(
    *,
    cycle_id: str,
    output_dir: str | Path,
    broker_profile: str = "paper",
    broker_profile_report: Mapping[str, Any] | None = None,
) -> Tuple[Any, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = dict(broker_profile_report or {})
    resolved_profile = str(report.get("broker_profile") or broker_profile or "paper")
    credential_source = str(report.get("credential_source") or f"{resolved_profile}_builtin")
    credential_valid = bool(report.get("credential_valid", resolved_profile != "real"))
    dry_run_only = bool(report.get("launch_switch", {}).get("real_broker_dry_run_only", True))
    session = BrokerSession(
        session_id=_session_id(cycle_id, resolved_profile),
        broker_profile=resolved_profile,
        capabilities=[
            "submit_order",
            "cancel_order",
            "get_order",
            "list_open_orders",
            "get_positions",
            "get_account",
            "flatten_all",
        ],
    )
    path = out / "broker_session.json"
    session_payload = session.to_dict()
    session_payload.update(
        {
            "credential_source": credential_source,
            "credential_valid": credential_valid,
            "dry_run_only": dry_run_only,
        }
    )
    path.write_text(json.dumps(session_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if resolved_profile == "real":
        adapter: Any = RealBrokerDryRunAdapter(
            session_id=session.session_id,
            broker_profile=resolved_profile,
            credential_source=credential_source,
            credential_valid=credential_valid,
            dry_run_only=dry_run_only,
        )
    else:
        adapter = PaperBrokerAdapter(session_id=session.session_id, broker_profile=resolved_profile)
    return adapter, str(path)


def run_real_broker_dry_run(
    *,
    cycle_id: str,
    broker: Any,
    broker_profile_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    report = dict(broker_profile_report or {})
    profile = str(report.get("broker_profile") or "paper")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "real_broker_dry_run.json"
    if profile != "real":
        payload = {
            "cycle_id": cycle_id,
            "broker_profile": profile,
            "status": "skipped",
            "reason": "profile_not_real",
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"real_broker_dry_run_json": str(path), "real_broker_dry_run": payload}

    connect_result = {}
    account = {}
    positions: list[Dict[str, Any]] = []
    open_orders: list[Dict[str, Any]] = []
    preflight_validation = {}
    try:
        connect_result = dict(getattr(broker, "connect")() or {})
        account = dict(getattr(broker, "get_account")() or {})
        positions = list(getattr(broker, "get_positions")() or [])
        open_orders = list(getattr(broker, "list_open_orders")() or [])
        validator = getattr(broker, "preflight_order_validation", None)
        if callable(validator):
            preflight_validation = dict(
                validator({"symbol": "SPY", "side": "BUY", "qty": 1, "price": 100.0}) or {}
            )
        else:
            preflight_validation = {"valid": False, "reasons": ["validator_missing"]}
    except Exception as exc:
        connect_result = {"ok": False, "status": "controlled_fail", "reason": f"exception:{exc}"}
        preflight_validation = {"valid": False, "reasons": ["dry_run_exception"]}

    connect_ok = bool(connect_result.get("ok", False))
    payload = {
        "cycle_id": cycle_id,
        "broker_profile": profile,
        "status": "success" if connect_ok else "controlled_fail",
        "connect": connect_result,
        "account_probe": {
            "ok": bool(account),
            "account": account,
        },
        "position_probe": {
            "ok": True,
            "positions": positions,
            "count": len(positions),
        },
        "open_orders_probe": {
            "ok": True,
            "orders": open_orders,
            "count": len(open_orders),
        },
        "preflight_order_validation": preflight_validation,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"real_broker_dry_run_json": str(path), "real_broker_dry_run": payload}


def build_broker_binding_report(
    *,
    cycle_id: str,
    broker_profile_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    report = dict(broker_profile_report or {})
    payload = {
        "cycle_id": cycle_id,
        "broker_profile": str(report.get("broker_profile") or "paper"),
        "binding_mode": "unified_broker_interface",
        "bindings": [
            {
                "capability": "broker_interface",
                "module": "scripts/ai_trader/research/broker_adapter.py",
                "reuse_mode": "direct",
                "fields": ["submit_order", "cancel_order", "get_order", "list_open_orders", "get_positions", "get_account", "flatten_all"],
            },
            {
                "capability": "oms_order_state",
                "module": "scripts/ai_trader/research/order_manager.py",
                "reuse_mode": "direct",
                "fields": ["client_order_id", "broker_order_id", "status", "history"],
            },
            {
                "capability": "account_positions_sync",
                "module": "scripts/ai_trader/research/account_sync.py|position_sync.py",
                "reuse_mode": "adapter",
                "fields": ["account", "positions", "exposure"],
            },
            {
                "capability": "risk_reconcile_kill",
                "module": "scripts/ai_trader/research/live_risk_enforcer.py|execution_reconcile.py|live_kill_switch.py",
                "reuse_mode": "direct",
                "fields": ["violations", "mismatches", "actions"],
            },
        ],
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_binding_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"broker_binding_report_json": str(path), "broker_binding_report": payload}


def persist_broker_session_state(
    *,
    broker_snapshot: Dict[str, Any],
    output_dir: str | Path,
) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_session_state.json"
    path.write_text(json.dumps(dict(broker_snapshot), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
