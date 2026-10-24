from __future__ import annotations

from typing import Any, Dict


def gate_evaluated_payload(
    *,
    symbol: str,
    exec_symbol: str | None,
    side: str | None,
    qty: int,
    gate_allowed: bool,
    gate_details: Dict[str, Any] | None,
    block_stage: str | None,
    trader_reason_code: str | None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "signal_symbol": str(symbol or "").strip().upper() or None,
        "exec_symbol": str(exec_symbol or "").strip().upper() or None,
        "side": str(side or "").strip().upper() or None,
        "qty": int(max(0, qty)),
        "gate_allowed": bool(gate_allowed),
        "gate_details": dict(gate_details or {}),
        "block_stage": str(block_stage or "").strip() or None,
    }
    if trader_reason_code:
        payload["trader_reason_code"] = str(trader_reason_code).strip().upper()
    return payload


def normalize_gate_details(
    gate_details: Dict[str, Any] | None,
    *,
    block_stage: str,
    reason: str,
    trader_reason_code: str,
) -> Dict[str, Any]:
    payload = dict(gate_details or {})
    payload.setdefault("_schema", "gate_details_v1")
    payload.setdefault("_block_stage", str(block_stage or "").strip())
    payload.setdefault("_reason", str(reason or "").strip())
    payload.setdefault("_trader_reason_code", str(trader_reason_code or "").strip().upper())
    return payload


def blocked_preview_payload(
    *,
    reason: str,
    trader_reason_code: str,
    gate_details: Dict[str, Any] | None,
    exec_symbol: str | None,
) -> Dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": str(reason or "").strip(),
        "trader_reason_code": str(trader_reason_code or "").strip().upper(),
        "gate_details": dict(gate_details or {}),
        "exec_symbol": exec_symbol,
    }


def block_stage_domain(block_stage: str) -> str:
    return "recon" if str(block_stage or "").strip() == "entry_blocked_reentry_cooldown" else "strategy"
