from __future__ import annotations

from typing import Any, Dict


def protection_order_payload(*, exec_symbol: str | None, order_ref: str | None, stop_px: float | None, live_qty: int) -> Dict[str, Any]:
    return {
        "exec_symbol": exec_symbol,
        "order_ref": order_ref,
        "stop_px": stop_px,
        "live_qty": int(max(0, live_qty)),
    }


def kill_switch_order_payload(
    *,
    exec_symbol: str | None,
    order_ref: str | None,
    qty: int | None,
    live_qty: int,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "exec_symbol": exec_symbol,
        "order_ref": order_ref,
        "live_qty": int(max(0, live_qty)),
    }
    if qty is not None:
        payload["qty"] = int(qty)
    return payload


def protection_stop_rebuilt_payload(
    *,
    exec_symbol: str | None,
    entry_px: float | None,
    entry_atr_used: float | None,
    stop_px: float | None,
    stop_dist_pre: float | None,
) -> Dict[str, Any]:
    return {
        "exec_symbol": exec_symbol,
        "entry_px": entry_px,
        "entry_atr_used": entry_atr_used,
        "stop_px": stop_px,
        "stop_dist_pre": stop_dist_pre,
    }
