from __future__ import annotations

from typing import Any, Dict, Mapping

from scripts.ai_trader.research.broker_models import ORDER_STATUSES, iso_now


ALLOWED_TRANSITIONS = {
    "created": {"submitted", "rejected", "expired"},
    "submitted": {"accepted", "rejected", "canceled", "expired"},
    "accepted": {"partially_filled", "filled", "canceled", "rejected", "expired"},
    "partially_filled": {"filled", "canceled", "expired"},
    "filled": set(),
    "canceled": set(),
    "rejected": set(),
    "expired": set(),
}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def init_order_state(
    *,
    client_order_id: str,
    candidate_id: str,
    experiment_id: str,
    symbol: str,
    side: str,
    qty: int,
    price: float,
) -> Dict[str, Any]:
    ts = iso_now()
    return {
        "client_order_id": client_order_id,
        "broker_order_id": "",
        "candidate_id": candidate_id,
        "experiment_id": experiment_id,
        "symbol": symbol,
        "side": side,
        "qty": int(qty),
        "price": float(price),
        "status": "created",
        "history": [
            {
                "from": None,
                "to": "created",
                "ts": ts,
                "reason": "order_initialized",
            }
        ],
    }


def apply_transition(
    *,
    order_state: Mapping[str, Any],
    to_status: str,
    reason: str,
    extra: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    state = _as_dict(order_state)
    history = list(state.get("history") or [])
    current = str(state.get("status") or "created")
    if to_status not in ORDER_STATUSES:
        raise ValueError(f"invalid status: {to_status}")
    if to_status != current and to_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid transition {current}->{to_status}")
    event = {"from": current, "to": to_status, "ts": iso_now(), "reason": reason}
    if extra:
        event["extra"] = dict(extra)
    history.append(event)
    state["status"] = to_status
    state["history"] = history
    return state

