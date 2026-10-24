from __future__ import annotations

import math
from typing import Any, Mapping

_DISCRETE_QTY_EPS = 1e-6
_DISCRETE_INSTRUMENT_TYPES = {
    "STK",
    "STOCK",
    "EQUITY",
    "OPTION",
    "OPT",
    "FUT",
    "FUTURE",
}


def instrument_type_from_context(*, symbol: str, extras: Mapping[str, Any] | None = None) -> str:
    extras = extras or {}
    instrument_type = str(extras.get("instrument_type") or "").strip().upper()
    if instrument_type:
        return instrument_type
    symbol_u = str(symbol or "").strip().upper()
    if symbol_u.startswith("OPT:"):
        return "OPTION"
    if symbol_u.startswith("FUT:"):
        return "FUT"
    return "STK"


def uses_discrete_quantity(*, symbol: str, extras: Mapping[str, Any] | None = None) -> bool:
    return instrument_type_from_context(symbol=symbol, extras=extras) in _DISCRETE_INSTRUMENT_TYPES


def stabilize_requested_qty(qty: float, *, symbol: str, extras: Mapping[str, Any] | None = None) -> float:
    qty_f = float(qty or 0.0)
    if not uses_discrete_quantity(symbol=symbol, extras=extras):
        return qty_f
    sign = -1.0 if qty_f < 0 else 1.0
    abs_qty = abs(qty_f)
    nearest = round(abs_qty)
    if abs(abs_qty - float(nearest)) <= _DISCRETE_QTY_EPS:
        return sign * float(nearest)
    return qty_f


def conservative_execution_qty(qty: float, *, symbol: str, extras: Mapping[str, Any] | None = None) -> float:
    qty_f = float(qty or 0.0)
    if not uses_discrete_quantity(symbol=symbol, extras=extras):
        return qty_f
    sign = -1.0 if qty_f < 0 else 1.0
    abs_qty = abs(qty_f)
    nearest = round(abs_qty)
    if abs(abs_qty - float(nearest)) <= _DISCRETE_QTY_EPS:
        normalized = float(nearest)
    else:
        normalized = float(math.floor(abs_qty + _DISCRETE_QTY_EPS))
    return sign * normalized
