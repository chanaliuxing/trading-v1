from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def _exists(path: str) -> bool:
    return Path(path).exists()


def _entry(
    *,
    capability: str,
    paths: List[str],
    reuse_mode: str,
    owner_layer: str,
    notes: str,
) -> Dict[str, Any]:
    return {
        "capability": capability,
        "exists": all(_exists(p) for p in paths) if paths else False,
        "path": paths,
        "reuse_mode": reuse_mode,
        "owner_layer": owner_layer,
        "notes": notes,
    }


def build_live_capability_inventory() -> Dict[str, Any]:
    items = [
        _entry(
            capability="broker_interface",
            paths=[
                "scripts/ai_trader/research/broker_adapter.py",
                "scripts/ai_trader/research/paper_broker_adapter.py",
                "scripts/ai_trader/research/broker_models.py",
            ],
            reuse_mode="adapter",
            owner_layer="live_execution",
            notes="paper broker and common broker interface are exposed for live path reuse",
        ),
        _entry(
            capability="order_submit_cancel",
            paths=[
                "scripts/ai_trader/research/paper_broker_adapter.py",
                "scripts/ai_trader/research/order_manager.py",
            ],
            reuse_mode="adapter",
            owner_layer="oms",
            notes="submit/cancel/get/list open orders are reused through paper broker adapter",
        ),
        _entry(
            capability="order_state_fill_audit",
            paths=[
                "scripts/ai_trader/research/order_state_machine.py",
                "scripts/ai_trader/research/order_manager.py",
            ],
            reuse_mode="adapter",
            owner_layer="oms",
            notes="order lifecycle transitions and events jsonl are directly generated from oms path",
        ),
        _entry(
            capability="account_snapshot",
            paths=["scripts/ai_trader/research/account_sync.py"],
            reuse_mode="adapter",
            owner_layer="risk_ops",
            notes="account snapshot sync wraps broker snapshot without replacing existing live executor",
        ),
        _entry(
            capability="positions_snapshot",
            paths=["scripts/ai_trader/research/position_sync.py"],
            reuse_mode="adapter",
            owner_layer="risk_ops",
            notes="position snapshot and reconcile report are generated from broker state + live manifests",
        ),
        _entry(
            capability="exposure_risk_guard",
            paths=[
                "scripts/ai_trader/research/live_risk_guard.py",
                "scripts/ai_trader/research/live_risk_enforcer.py",
            ],
            reuse_mode="direct",
            owner_layer="risk_guard",
            notes="pre-trade guard is reused directly; post-trade enforcement is thin extension",
        ),
        _entry(
            capability="reconcile_parity",
            paths=[
                "scripts/ai_trader/research/execution_feedback.py",
                "scripts/ai_trader/research/execution_reconcile.py",
            ],
            reuse_mode="adapter",
            owner_layer="reconcile",
            notes="existing execution feedback is reused, with thin mismatch classification adapter",
        ),
        _entry(
            capability="kill_switch_flatten",
            paths=[
                "scripts/ai_trader/research/live_risk_guard.py",
                "scripts/ai_trader/research/session_control.py",
                "scripts/ai_trader/research/live_kill_switch.py",
            ],
            reuse_mode="direct",
            owner_layer="risk_controls",
            notes="global/candidate/symbol kill paths reuse operator controls and risk events",
        ),
        _entry(
            capability="recovery_restart",
            paths=[
                "scripts/ai_trader/research/cycle_state.py",
                "scripts/ai_trader/research/recovery_playbook.py",
            ],
            reuse_mode="direct",
            owner_layer="runtime_recovery",
            notes="existing cycle resume/restart plus recovery playbook for broker/account/order snapshots",
        ),
        _entry(
            capability="real_broker_sdk_profile",
            paths=[],
            reuse_mode="missing",
            owner_layer="broker_connectivity",
            notes="real broker SDK adapter is intentionally not wired yet; paper adapter is current path",
        ),
    ]
    return {"items": items}


def build_live_reuse_plan(*, inventory: Dict[str, Any]) -> Dict[str, Any]:
    plan_items = []
    for row in inventory.get("items", []):
        cap = str(row.get("capability") or "")
        mode = str(row.get("reuse_mode") or "missing")
        if mode == "missing":
            plan_items.append(
                {
                    "capability": cap,
                    "action": "gap",
                    "reuse_target": [],
                    "field_mapping": {},
                    "thin_adapter_needed": True,
                    "notes": "missing in current repo path; keep as explicit gap only",
                }
            )
            continue
        if cap == "broker_interface":
            field_mapping = {"candidate_manifest.broker_profile": "broker_session.broker_profile"}
        elif cap == "order_state_fill_audit":
            field_mapping = {
                "client_order_id": "orders_state.client_order_id",
                "broker_order_id": "orders_state.broker_order_id",
                "history": "orders_events.jsonl",
            }
        elif cap == "account_snapshot":
            field_mapping = {"broker_snapshot.account": "account_snapshot"}
        elif cap == "positions_snapshot":
            field_mapping = {"broker_snapshot.positions": "positions_snapshot.positions"}
        elif cap == "reconcile_parity":
            field_mapping = {
                "orders_state.items": "execution_reconcile.expected_orders",
                "broker_snapshot.orders/fills": "execution_reconcile.actual",
            }
        else:
            field_mapping = {}
        plan_items.append(
            {
                "capability": cap,
                "action": "reuse",
                "reuse_target": row.get("path", []),
                "field_mapping": field_mapping,
                "thin_adapter_needed": mode == "adapter",
                "notes": row.get("notes", ""),
            }
        )
    return {"items": plan_items}


def build_live_integration_report(
    *,
    cycle_id: str,
    inventory: Dict[str, Any],
    reuse_plan: Dict[str, Any],
) -> Dict[str, Any]:
    inventory_items = inventory.get("items", [])
    direct_or_adapter = [
        x
        for x in inventory_items
        if str(x.get("reuse_mode") or "") in {"direct", "adapter"} and bool(x.get("exists"))
    ]
    missing = [x for x in inventory_items if str(x.get("reuse_mode") or "") == "missing"]
    return {
        "cycle_id": cycle_id,
        "integration_mode": "reuse_first",
        "reused_capabilities": [x.get("capability") for x in direct_or_adapter],
        "adapter_capabilities": [
            x.get("capability")
            for x in direct_or_adapter
            if str(x.get("reuse_mode") or "") == "adapter"
        ],
        "missing_capabilities": [x.get("capability") for x in missing],
        "reuse_plan_items": len(reuse_plan.get("items", [])),
        "status": "ok",
    }


def build_live_gap_list(*, inventory: Dict[str, Any]) -> Dict[str, Any]:
    gaps = []
    for row in inventory.get("items", []):
        if str(row.get("reuse_mode") or "") != "missing":
            continue
        cap = str(row.get("capability") or "")
        if cap == "real_broker_sdk_profile":
            reason = "cannot be reused from existing code because no real broker SDK binding/profile exists in repo"
            must = "add one thin real broker adapter implementing current broker interface"
        else:
            reason = "not found in existing live path"
            must = "add minimal thin adapter only"
        gaps.append(
            {
                "capability": cap,
                "why_not_reuse": reason,
                "minimal_required": must,
            }
        )
    return {"items": gaps}


def generate_live_reuse_artifacts(
    *,
    cycle_id: str,
    output_dir: str | Path,
) -> Dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    inventory = build_live_capability_inventory()
    plan = build_live_reuse_plan(inventory=inventory)
    integration = build_live_integration_report(cycle_id=cycle_id, inventory=inventory, reuse_plan=plan)
    gaps = build_live_gap_list(inventory=inventory)

    inventory_path = out / "live_capability_inventory.json"
    reuse_plan_path = out / "live_reuse_plan.json"
    integration_path = out / "live_integration_report.json"
    gap_path = out / "live_gap_list.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    reuse_plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    integration_path.write_text(json.dumps(integration, indent=2, ensure_ascii=False), encoding="utf-8")
    gap_path.write_text(json.dumps(gaps, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "live_capability_inventory_json": str(inventory_path),
        "live_reuse_plan_json": str(reuse_plan_path),
        "live_integration_report_json": str(integration_path),
        "live_gap_list_json": str(gap_path),
    }

