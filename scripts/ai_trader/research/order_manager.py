from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.broker_adapter import (
    build_broker_binding_report,
    create_broker_session,
    persist_broker_session_state,
    run_real_broker_dry_run,
)
from scripts.ai_trader.research.broker_profile import (
    build_broker_profile_report,
    persist_broker_launch_switch,
)
from scripts.ai_trader.research.order_state_machine import (
    apply_transition,
    init_order_state,
)


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


def run_order_manager(
    *,
    cycle_id: str,
    live_candidates_dir: str | Path,
    risk_guard_report: Mapping[str, Any],
    output_dir: str | Path,
    broker_profile: str = "paper",
    operator_state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    live_dir = Path(live_candidates_dir)
    profile_bundle = build_broker_profile_report(
        cycle_id=cycle_id,
        requested_profile=broker_profile,
        output_dir=out,
        state_root=operator_state_root,
    )
    launch_switch_path = persist_broker_launch_switch(
        profile_bundle.get("launch_switch") or {},
        output_dir=out,
        state_root=operator_state_root,
    )
    broker_binding = build_broker_binding_report(
        cycle_id=cycle_id,
        broker_profile_report=_as_dict(profile_bundle.get("broker_profile_report")),
        output_dir=out,
    )
    broker, broker_session_path = create_broker_session(
        cycle_id=cycle_id,
        output_dir=out,
        broker_profile=broker_profile,
        broker_profile_report=_as_dict(profile_bundle.get("broker_profile_report")),
    )
    dry_run_bundle = run_real_broker_dry_run(
        cycle_id=cycle_id,
        broker=broker,
        broker_profile_report=_as_dict(profile_bundle.get("broker_profile_report")),
        output_dir=out,
    )
    effective_profile = str(_as_dict(profile_bundle.get("broker_profile_report")).get("broker_profile") or "paper")

    risk_by_candidate = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    orders_state: list[Dict[str, Any]] = []
    events: list[Dict[str, Any]] = []
    sequence = 0
    for candidate_dir in sorted(live_dir.iterdir()) if live_dir.exists() else []:
        if not candidate_dir.is_dir():
            continue
        manifest_path = candidate_dir / "live_manifest.json"
        if not manifest_path.exists():
            continue
        manifest = _as_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        candidate_id = str(manifest.get("candidate_id") or candidate_dir.name)
        experiment_id = str(manifest.get("experiment_id") or candidate_id)
        symbols = [str(x).upper() for x in _as_list(manifest.get("symbols")) if str(x).strip()]
        if not symbols:
            symbols = ["SPY"]
        if len(symbols) == 1:
            symbols = symbols + symbols
        allowed = bool(_as_dict(risk_by_candidate.get(candidate_id)).get("allowed", True))
        for idx, symbol in enumerate(symbols[:2], start=1):
            sequence += 1
            client_order_id = f"{candidate_id}-cli-{sequence:05d}"
            state = init_order_state(
                client_order_id=client_order_id,
                candidate_id=candidate_id,
                experiment_id=experiment_id,
                symbol=symbol,
                side="BUY",
                qty=idx + 1,
                price=100.0 + idx,
            )
            state = apply_transition(order_state=state, to_status="submitted", reason="oms_submit")
            if not allowed:
                state = apply_transition(order_state=state, to_status="rejected", reason="risk_enforcer_pre_block")
            elif effective_profile == "real":
                # Real profile path is dry-run-only by default: validate but do not submit.
                state = apply_transition(order_state=state, to_status="rejected", reason="real_broker_dry_run_only")
            else:
                state = apply_transition(order_state=state, to_status="accepted", reason="broker_accept")
                behavior = "filled" if idx == 1 else "partial"
                broker_order = broker.submit_order(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side="BUY",
                    qty=int(state["qty"]),
                    price=float(state["price"]),
                    candidate_id=candidate_id,
                    experiment_id=experiment_id,
                    behavior=behavior,
                )
                state["broker_order_id"] = str(_as_dict(broker_order).get("broker_order_id") or "")
                broker_status = str(_as_dict(broker_order).get("status") or "rejected")
                if broker_status == "filled":
                    state = apply_transition(order_state=state, to_status="filled", reason="broker_filled")
                elif broker_status == "partially_filled":
                    state = apply_transition(order_state=state, to_status="partially_filled", reason="broker_partial_fill")
                elif broker_status == "rejected":
                    state = apply_transition(order_state=state, to_status="rejected", reason="broker_rejected")
            orders_state.append(state)
            for ev in state.get("history", []):
                row = dict(ev)
                row["client_order_id"] = state["client_order_id"]
                row["broker_order_id"] = state.get("broker_order_id") or ""
                row["candidate_id"] = candidate_id
                row["experiment_id"] = experiment_id
                row["symbol"] = symbol
                events.append(row)

    orders_state_path = out / "orders_state.json"
    orders_events_path = out / "orders_events.jsonl"
    orders_state_path.write_text(json.dumps({"cycle_id": cycle_id, "items": orders_state}, indent=2, ensure_ascii=False), encoding="utf-8")
    with orders_events_path.open("w", encoding="utf-8") as fh:
        for row in events:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    broker_snapshot = broker.export_state()
    broker_state_path = persist_broker_session_state(
        broker_snapshot=broker_snapshot,
        output_dir=out,
    )
    return {
        "broker_adapter": broker,
        "broker_session_json": broker_session_path,
        "broker_session_state_json": broker_state_path,
        "broker_profile_report_json": str(profile_bundle["broker_profile_report_json"]),
        "broker_binding_report_json": str(broker_binding["broker_binding_report_json"]),
        "broker_launch_switch_json": str(launch_switch_path),
        "real_broker_dry_run_json": str(dry_run_bundle["real_broker_dry_run_json"]),
        "orders_state_json": str(orders_state_path),
        "orders_events_jsonl": str(orders_events_path),
        "orders_state": {"cycle_id": cycle_id, "items": orders_state},
        "broker_snapshot": broker_snapshot,
        "broker_profile_report": _as_dict(profile_bundle.get("broker_profile_report")),
        "broker_binding_report": _as_dict(broker_binding.get("broker_binding_report")),
        "real_broker_dry_run": _as_dict(dry_run_bundle.get("real_broker_dry_run")),
    }
