from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.broker_models import BrokerSession
from scripts.ai_trader.research.paper_broker_adapter import PaperBrokerAdapter


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_broker_sandbox_orders(
    *,
    cycle_id: str,
    broker_profile_report: Mapping[str, Any],
    broker_safety_gate: Mapping[str, Any],
    operator_controls: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
    capital_governor_report: Mapping[str, Any],
    session_control_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    orders_path = out / "broker_sandbox_orders.json"
    events_path = out / "broker_sandbox_events.jsonl"

    profile = _as_dict(broker_profile_report)
    gate = _as_dict(broker_safety_gate)
    controls = _as_dict(operator_controls)
    launch_switch = _as_dict(profile.get("launch_switch"))
    risk_items = [_as_dict(x) for x in _as_dict(risk_guard_report).get("items", []) if isinstance(x, dict)]
    risk_by_candidate = {str(x.get("candidate_id") or ""): x for x in risk_items}
    cap_items = [_as_dict(x) for x in _as_dict(capital_governor_report).get("items", []) if isinstance(x, dict)]
    cap_by_candidate = {str(x.get("candidate_id") or ""): x for x in cap_items}
    sess_items = [_as_dict(x) for x in _as_dict(session_control_report).get("items", []) if isinstance(x, dict)]
    sess_by_candidate = {str(x.get("candidate_id") or ""): x for x in sess_items}

    can_sandbox = (
        bool(controls.get("live_enabled", False))
        and not bool(controls.get("global_pause", False))
        and bool(launch_switch.get("real_broker_dry_run_only", True))
        and (
            str(gate.get("gate_status") or "") in {"approved", "not_applicable"}
            or str(profile.get("broker_profile") or "") != "real"
        )
    )

    session = BrokerSession(
        session_id=f"sandbox-{cycle_id}",
        broker_profile="sandbox",
        capabilities=["submit_order", "cancel_order", "get_order", "list_open_orders"],
    )
    broker = PaperBrokerAdapter(session_id=session.session_id, broker_profile="sandbox")
    candidate_id = "sandbox-candidate"
    experiment_id = "sandbox-experiment"
    submitted: list[Dict[str, Any]] = []
    canceled: list[Dict[str, Any]] = []
    rejected: list[Dict[str, Any]] = []
    events: list[Dict[str, Any]] = []

    if can_sandbox:
        # success submit -> filled
        ok_order = broker.submit_order(
            client_order_id=f"{candidate_id}-ok-001",
            symbol="SPY",
            side="BUY",
            qty=2,
            price=100.0,
            candidate_id=candidate_id,
            experiment_id=experiment_id,
            behavior="filled",
        )
        submitted.append(ok_order)
        events.append({"event": "submit", "status": ok_order.get("status"), "client_order_id": ok_order.get("client_order_id"), "ts": _iso_now()})

        # cancel sample
        pending = broker.submit_order(
            client_order_id=f"{candidate_id}-pending-001",
            symbol="QQQ",
            side="BUY",
            qty=1,
            price=101.0,
            candidate_id=candidate_id,
            experiment_id=experiment_id,
            behavior="rejected",
        )
        cancel_result = broker.cancel_order(broker_order_id=str(pending.get("broker_order_id") or ""))
        canceled.append(_as_dict(cancel_result))
        events.append({"event": "cancel", "status": "ok" if bool(cancel_result.get("ok")) else "failed", "broker_order_id": pending.get("broker_order_id"), "ts": _iso_now()})

        # reject sample by guards
        risk_row = _as_dict(risk_by_candidate.get(candidate_id))
        cap_row = _as_dict(cap_by_candidate.get(candidate_id))
        sess_row = _as_dict(sess_by_candidate.get(candidate_id))
        reject_reasons = []
        if risk_row and not bool(risk_row.get("allowed", True)):
            reject_reasons.append("risk_guard_blocked")
        if str(cap_row.get("decision") or "") == "rejected":
            reject_reasons.append("capital_governor_rejected")
        if sess_row and not bool(sess_row.get("session_allowed", True)):
            reject_reasons.append("session_blocked")
        if not reject_reasons:
            reject_reasons.append("sandbox_policy_reject_sample")
        rej = {
            "client_order_id": f"{candidate_id}-rej-001",
            "status": "rejected",
            "reasons": reject_reasons,
            "symbol": "IWM",
        }
        rejected.append(rej)
        events.append({"event": "reject", "status": "rejected", "client_order_id": rej["client_order_id"], "reasons": reject_reasons, "ts": _iso_now()})
    else:
        rejected.append(
            {
                "client_order_id": f"{candidate_id}-rej-guard",
                "status": "rejected",
                "reasons": ["sandbox_not_allowed_by_controls"],
                "symbol": "SPY",
            }
        )
        events.append({"event": "reject", "status": "rejected", "reason": "sandbox_not_allowed_by_controls", "ts": _iso_now()})

    payload = {
        "cycle_id": cycle_id,
        "broker_profile": str(profile.get("broker_profile") or "paper"),
        "sandbox_allowed": can_sandbox,
        "submitted": submitted,
        "canceled": canceled,
        "rejected": rejected,
        "summary": {
            "submitted_count": len(submitted),
            "canceled_count": len(canceled),
            "rejected_count": len(rejected),
        },
    }
    orders_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with events_path.open("w", encoding="utf-8") as fh:
        for row in events:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "broker_sandbox_orders_json": str(orders_path),
        "broker_sandbox_events_jsonl": str(events_path),
        "broker_sandbox_orders": payload,
    }

