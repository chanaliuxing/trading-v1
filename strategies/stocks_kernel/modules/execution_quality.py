from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionIntentRecord:
    order_ref: str
    symbol: str
    side: str
    order_role: str
    expected_price: float | None
    trigger_price: float | None
    requested_ts_epoch_s: float
    submit_ack_ts_epoch_s: float | None = None


class ExecutionQualityModule:
    """
    Compute execution quality metrics from intent/ack/fill timestamps and prices.
    """

    def __init__(self) -> None:
        self._intents: dict[str, ExecutionIntentRecord] = {}

    def register_intent(
        self,
        *,
        order_ref: str,
        symbol: str,
        side: str,
        order_role: str,
        expected_price: float | None,
        trigger_price: float | None,
        now_ts_epoch_s: float,
    ) -> None:
        order_ref_v = str(order_ref or "").strip()
        if not order_ref_v:
            return
        self._intents[order_ref_v] = ExecutionIntentRecord(
            order_ref=order_ref_v,
            symbol=str(symbol or "").strip().upper(),
            side=str(side or "").strip().upper(),
            order_role=str(order_role or "").strip().upper(),
            expected_price=float(expected_price) if expected_price is not None else None,
            trigger_price=float(trigger_price) if trigger_price is not None else None,
            requested_ts_epoch_s=float(now_ts_epoch_s or 0.0),
        )

    def register_submit_ack(self, *, order_ref: str, now_ts_epoch_s: float) -> dict[str, Any]:
        order_ref_v = str(order_ref or "").strip()
        if not order_ref_v:
            return {}
        record = self._intents.get(order_ref_v)
        if record is None:
            return {}
        record = ExecutionIntentRecord(
            order_ref=record.order_ref,
            symbol=record.symbol,
            side=record.side,
            order_role=record.order_role,
            expected_price=record.expected_price,
            trigger_price=record.trigger_price,
            requested_ts_epoch_s=record.requested_ts_epoch_s,
            submit_ack_ts_epoch_s=float(now_ts_epoch_s or 0.0),
        )
        self._intents[order_ref_v] = record
        ack_latency_ms = None
        if record.requested_ts_epoch_s > 0.0 and record.submit_ack_ts_epoch_s and record.submit_ack_ts_epoch_s > 0.0:
            ack_latency_ms = max(0.0, (record.submit_ack_ts_epoch_s - record.requested_ts_epoch_s) * 1000.0)
        return {
            "order_ref": order_ref_v,
            "symbol": record.symbol,
            "order_role": record.order_role,
            "submit_ack_latency_ms": ack_latency_ms,
        }

    def register_fill(
        self,
        *,
        order_ref: str | None,
        symbol: str,
        order_role: str,
        side: str,
        fill_price: float | None,
        fill_qty: float,
        now_ts_epoch_s: float,
    ) -> dict[str, Any]:
        order_ref_v = str(order_ref or "").strip()
        record = self._intents.get(order_ref_v) if order_ref_v else None
        side_u = str(side or "").strip().upper()
        expected_price = record.expected_price if record is not None else None
        trigger_price = record.trigger_price if record is not None else None
        submit_ack_ts = record.submit_ack_ts_epoch_s if record is not None else None
        requested_ts = record.requested_ts_epoch_s if record is not None else None
        fill_latency_ms = None
        if submit_ack_ts and submit_ack_ts > 0.0 and now_ts_epoch_s > submit_ack_ts:
            fill_latency_ms = max(0.0, (float(now_ts_epoch_s) - submit_ack_ts) * 1000.0)
        end_to_end_ms = None
        if requested_ts and requested_ts > 0.0 and now_ts_epoch_s > requested_ts:
            end_to_end_ms = max(0.0, (float(now_ts_epoch_s) - requested_ts) * 1000.0)
        slippage_bps = self._compute_slippage_bps(
            expected_price=expected_price,
            fill_price=fill_price,
            side=side_u,
            order_role=str(order_role or "").strip().upper(),
        )
        grade = self._grade(slippage_bps=slippage_bps, fill_latency_ms=fill_latency_ms)
        payload = {
            "order_ref": order_ref_v or None,
            "symbol": str(symbol or "").strip().upper(),
            "order_role": str(order_role or "").strip().upper(),
            "side": side_u,
            "expected_price": expected_price,
            "trigger_price": trigger_price,
            "fill_price": float(fill_price) if fill_price is not None else None,
            "fill_qty": float(fill_qty or 0.0),
            "slippage_bps": slippage_bps,
            "submit_to_fill_latency_ms": fill_latency_ms,
            "intent_to_fill_latency_ms": end_to_end_ms,
            "execution_quality_grade": grade,
        }
        if record is not None and order_ref_v:
            self._intents.pop(order_ref_v, None)
        return payload

    @staticmethod
    def _compute_slippage_bps(
        *,
        expected_price: float | None,
        fill_price: float | None,
        side: str,
        order_role: str,
    ) -> float | None:
        if expected_price is None or fill_price is None:
            return None
        exp_px = float(expected_price)
        fill_px = float(fill_price)
        if exp_px <= 0.0:
            return None
        side_sign = 1.0 if str(side or "").upper() == "LONG" else -1.0
        role_u = str(order_role or "").upper()
        if role_u in {"FLATTEN", "TAKE_PROFIT", "RETREAT", "REDUCE", "STOP"}:
            side_sign *= -1.0
        adverse = (fill_px - exp_px) * side_sign
        return (adverse / exp_px) * 10_000.0

    @staticmethod
    def _grade(*, slippage_bps: float | None, fill_latency_ms: float | None) -> str:
        if slippage_bps is None:
            return "unknown"
        abs_slip = abs(float(slippage_bps))
        latency = float(fill_latency_ms or 0.0)
        if abs_slip <= 5.0 and latency <= 1000.0:
            return "A"
        if abs_slip <= 12.0 and latency <= 3000.0:
            return "B"
        if abs_slip <= 20.0 and latency <= 6000.0:
            return "C"
        return "D"
