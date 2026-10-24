from __future__ import annotations

from typing import Any, Optional


def exit_target_qty(*, qty_signed: float, qty_to_exit: Optional[int] = None) -> float:
    signed = float(qty_signed or 0.0)
    if qty_to_exit is None:
        return float(-signed)
    qty = int(max(0, int(qty_to_exit)))
    return float(-qty if signed > 0 else qty)


def build_close_only_extras(
    *,
    reason: str,
    trigger_id: str | None,
    trace_id: str,
    order_ref: str,
    position_qty_hint: float,
    extra_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reason": reason,
        "trigger_id": trigger_id,
        "trace_id": trace_id,
        "disable_oco": True,
        "force_order_style": True,
        "close_only": True,
        "position_qty_hint": float(position_qty_hint),
        "position_qty_hint_source": "strategy_runtime",
        "order_ref": order_ref,
    }
    for key, value in dict(extra_flags or {}).items():
        payload[str(key)] = value
    return payload
