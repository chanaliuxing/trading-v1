from __future__ import annotations

from typing import Any, Iterable


def terminal_order_state(order: Any) -> bool:
    state = str(getattr(order, "state", "") or "").strip().replace(" ", "").replace("_", "").upper()
    return state in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "INACTIVE", "APICANCELLED"}


def cancel_id_from_order(order: Any) -> int | None:
    for field in ("broker_order_id", "order_id", "id"):
        raw = getattr(order, field, None)
        if raw is None:
            continue
        try:
            text = str(raw).strip()
            if not text:
                continue
            return int(text)
        except Exception:
            continue
    return None


def matches_any_namespace_prefix(order_ref: str, prefixes: Iterable[str]) -> bool:
    ref = str(order_ref or "").strip()
    return any(ref.startswith(str(prefix or "")) for prefix in prefixes)


def order_symbol(order: Any) -> str:
    return str(getattr(order, "symbol", "") or "").strip().upper()


def order_strategy_id(order: Any) -> str:
    return str(getattr(order, "strategy_id", "") or "").strip()


def order_ref(order: Any) -> str:
    return str(getattr(order, "order_ref", "") or "").strip()
