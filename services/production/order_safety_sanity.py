from __future__ import annotations

import json
from typing import Any


SAFETY_REF_TOKENS = (
    "protect",
    "protection",
    "stop",
    "close",
    "flatten",
    "recovery",
    "eod",
    "manual_flatten",
    ":fla",
    "fla",
    ":sl",
    ":tp",
    "sl:",
    "tp:",
)

SAFETY_NOTE_FLAGS = {
    "protection",
    "protective_stop",
    "exit",
    "close_only",
    "flatten_action",
    "recovery_action",
    "runtime_recovery_action",
    "eod_flatten",
    "manual_intervention",
}


def positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if number > 0:
        return number
    return None


def get_value(order: Any, *names: str) -> Any:
    for name in names:
        if isinstance(order, dict) and name in order:
            return order.get(name)
        if hasattr(order, name):
            return getattr(order, name)
    nested = getattr(order, "order", None)
    if nested is not None:
        for name in names:
            if hasattr(nested, name):
                return getattr(nested, name)
    return None


def parse_note(note_raw: Any) -> dict[str, Any]:
    if isinstance(note_raw, dict):
        return dict(note_raw)
    if isinstance(note_raw, str) and note_raw.strip():
        try:
            parsed = json.loads(note_raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def is_safety_like_order(order: Any) -> bool:
    note = parse_note(get_value(order, "note"))
    for key in SAFETY_NOTE_FLAGS:
        if bool(note.get(key)):
            return True
    if isinstance(note.get("flatten_lifecycle"), dict):
        return True
    if str(note.get("recovery_intent_id") or "").strip():
        return True
    order_ref = str(get_value(order, "order_ref", "orderRef") or "").strip().lower()
    return any(token in order_ref for token in SAFETY_REF_TOKENS)


def order_limit_px(order: Any) -> float | None:
    return positive_float(get_value(order, "limit_px", "limit_price", "lmtPrice"))


def order_stop_px(order: Any) -> float | None:
    return positive_float(get_value(order, "stop_px", "aux_px", "auxPrice"))


def price_bearing_values(order: Any) -> list[float]:
    values: list[float] = []
    for value in (order_limit_px(order), order_stop_px(order)):
        if value is not None:
            values.append(value)
    return values


def validate_market_price_band(
    *,
    order: Any,
    market_ref_px: float | None,
    max_deviation_bps: float = 3500.0,
    require_ref_for_priced_order: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    prices = price_bearing_values(order)
    if not prices:
        return True, "not_price_bearing", {}
    market_ref = positive_float(market_ref_px)
    if market_ref is None:
        return (
            not require_ref_for_priced_order,
            "missing_market_ref_for_priced_safety_order",
            {"prices": prices},
        )
    max_dev = max(0.01, float(max_deviation_bps or 3500.0) / 10000.0)
    low = market_ref * (1.0 - max_dev)
    high = market_ref * (1.0 + max_dev)
    for price in prices:
        if price < low or price > high:
            dev_bps = abs(price - market_ref) / market_ref * 10000.0
            return (
                False,
                "safety_order_price_outside_market_band",
                {
                    "price": price,
                    "market_ref_px": market_ref,
                    "max_deviation_bps": float(max_deviation_bps or 3500.0),
                    "deviation_bps": dev_bps,
                    "low": low,
                    "high": high,
                },
            )
    return True, "price_within_market_band", {"market_ref_px": market_ref, "prices": prices}
