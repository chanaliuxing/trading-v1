from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.paper_broker_adapter import PaperBrokerAdapter


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_canary_executor(
    *,
    cycle_id: str,
    candidate_id: str,
    symbol: str,
    canary_launch_gate: Mapping[str, Any],
    manual_approval_report: Mapping[str, Any],
    output_dir: str | Path,
    max_default_capital: float = 500.0,
) -> Dict[str, Any]:
    out = Path(output_dir)
    root = out / "canary_runs"
    root.mkdir(parents=True, exist_ok=True)
    gate = _as_dict(canary_launch_gate)
    approval = _as_dict(_as_dict(manual_approval_report).get("manual_approval"))
    gate_ok = str(gate.get("gate_status") or "") == "approved"
    max_capital = min(float(approval.get("max_live_capital") or max_default_capital), max_default_capital)
    target_symbol = str(symbol or "SPY").upper()

    run_dir = root / candidate_id
    run_dir.mkdir(parents=True, exist_ok=True)
    orders_path = run_dir / "orders.jsonl"
    fills_path = run_dir / "fills.jsonl"
    summary_path = run_dir / "canary_run_summary.json"

    orders: list[Dict[str, Any]] = []
    fills: list[Dict[str, Any]] = []
    broker = PaperBrokerAdapter(session_id=f"canary-{cycle_id}", broker_profile="canary")
    if gate_ok:
        order = broker.submit_order(
            client_order_id=f"{candidate_id}-canary-001",
            symbol=target_symbol,
            side="BUY",
            qty=1,
            price=100.0,
            candidate_id=candidate_id,
            experiment_id="canary",
            behavior="filled",
        )
        orders.append(order)
        fills = list(_as_dict(broker.export_state()).get("fills") or [])
        status = "executed"
        reasons = ["gate_approved"]
    else:
        orders.append(
            {
                "client_order_id": f"{candidate_id}-canary-reject-001",
                "status": "rejected",
                "symbol": target_symbol,
                "reason": "canary_gate_rejected",
            }
        )
        status = "blocked"
        reasons = _as_dict(gate).get("reasons") or ["canary_gate_rejected"]

    with orders_path.open("w", encoding="utf-8") as fh:
        for row in orders:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with fills_path.open("w", encoding="utf-8") as fh:
        for row in fills:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    blocked_dir = root / f"{candidate_id}-blocked"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    blocked_summary = {
        "cycle_id": cycle_id,
        "candidate_id": f"{candidate_id}-blocked",
        "execution_status": "blocked",
        "reasons": ["gate_rejected_sample"],
    }
    (blocked_dir / "canary_run_summary.json").write_text(json.dumps(blocked_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (blocked_dir / "orders.jsonl").write_text(json.dumps({"status": "rejected", "reason": "gate_rejected_sample"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (blocked_dir / "fills.jsonl").write_text("", encoding="utf-8")

    summary = {
        "cycle_id": cycle_id,
        "candidate_id": candidate_id,
        "symbol": target_symbol,
        "max_live_capital": max_capital,
        "execution_status": status,
        "orders_total": len(orders),
        "fills_total": len(fills),
        "reasons": reasons,
        "started_at": _iso_now(),
        "completed_at": _iso_now(),
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "canary_runs_dir": str(root),
        "canary_run_summary_json": str(summary_path),
        "canary_orders_jsonl": str(orders_path),
        "canary_fills_jsonl": str(fills_path),
        "canary_run_summary": summary,
    }

