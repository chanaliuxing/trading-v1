from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional


ProtectionMode = Literal[
    "FIXED_STP",
    "RATCHET_STP",
    "NATIVE_TRAIL",
    "NATIVE_TRAIL_LIMIT",
    "EMERGENCY_EXIT",
]


@dataclass
class ProtectionSpec:
    symbol: str
    position_id: str
    side: Literal["LONG", "SHORT"]
    qty: Decimal
    entry_px: float
    current_px: float
    initial_stop_px: float
    initial_risk_per_share: float
    con_id: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    tp1_filled: bool = False
    runner_qty: Optional[Decimal] = None
    highest_px: Optional[float] = None
    lowest_px: Optional[float] = None
    mode: ProtectionMode = "FIXED_STP"
    desired_stop_px: Optional[float] = None
    desired_trail_amount: Optional[float] = None
    desired_trail_percent: Optional[float] = None
    desired_limit_offset: Optional[float] = None
    outside_rth: bool = False
    close_only: bool = True
    reduce_only: bool = True


@dataclass
class ProtectiveOrderState:
    position_id: str
    symbol: str
    order_type: Literal["STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT"]
    action: Literal["SELL", "BUY"]
    qty: Decimal
    ib_order_id: Optional[int] = None
    ib_perm_id: Optional[int] = None
    stop_px: Optional[float] = None
    limit_px: Optional[float] = None
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    trail_stop_px: Optional[float] = None
    limit_offset: Optional[float] = None
    status: Optional[str] = None
    oca_group: Optional[str] = None
    order_ref: Optional[str] = None
    last_amend_ts: Optional[float] = None
    last_seen_ts: Optional[float] = None
    triggered_ts: Optional[float] = None


def close_action_for_side(side: str) -> str:
    side_u = str(side or "").strip().upper()
    if side_u == "LONG":
        return "SELL"
    if side_u == "SHORT":
        return "BUY"
    raise ValueError(f"unsupported protection side: {side}")


def order_type_for_mode(mode: str) -> str:
    mode_u = str(mode or "").strip().upper()
    if mode_u in {"FIXED_STP", "RATCHET_STP"}:
        return "STP"
    if mode_u == "NATIVE_TRAIL":
        return "TRAIL"
    if mode_u == "NATIVE_TRAIL_LIMIT":
        return "TRAIL_LIMIT"
    if mode_u == "EMERGENCY_EXIT":
        return "MKT"
    raise ValueError(f"unsupported protection mode: {mode}")


class ProtectionEngine:
    """Stateless protection-order policy helpers used by execution safety paths."""

    @staticmethod
    def validate_replace(
        *,
        side: str,
        action: str,
        qty: float,
        position_qty: float,
        old_stop_px: Optional[float] = None,
        new_stop_px: Optional[float] = None,
        manual_override: bool = False,
        emergency: bool = False,
    ) -> None:
        expected_action = close_action_for_side(side)
        if str(action or "").strip().upper() != expected_action:
            raise ValueError(f"protective order action must be {expected_action} for {side}")
        if abs(float(qty or 0.0)) > abs(float(position_qty or 0.0)) + 1e-9:
            raise ValueError("protective order qty exceeds current position qty")
        if manual_override or emergency or old_stop_px is None or new_stop_px is None:
            return
        side_u = str(side or "").strip().upper()
        if side_u == "LONG" and float(new_stop_px) < float(old_stop_px) - 1e-9:
            raise ValueError("long protective stop cannot move down")
        if side_u == "SHORT" and float(new_stop_px) > float(old_stop_px) + 1e-9:
            raise ValueError("short protective stop cannot move up")

    @staticmethod
    def trail_limit_triggered(
        *,
        side: str,
        last_px: float,
        trail_stop_px: Optional[float],
    ) -> bool:
        if trail_stop_px is None:
            return False
        side_u = str(side or "").strip().upper()
        if side_u == "LONG":
            return float(last_px) <= float(trail_stop_px)
        if side_u == "SHORT":
            return float(last_px) >= float(trail_stop_px)
        return False

    @staticmethod
    def trail_limit_needs_fallback(
        *,
        order: ProtectiveOrderState,
        now_ts: Optional[float] = None,
        timeout_sec: float = 3.0,
    ) -> bool:
        if str(order.order_type or "").upper() != "TRAIL_LIMIT":
            return False
        if str(order.status or "").strip().upper() in {"FILLED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}:
            return False
        if order.triggered_ts is None:
            return False
        return float(now_ts if now_ts is not None else time.time()) - float(order.triggered_ts) >= float(timeout_sec)
