from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from libs.strategy.base import DecisionEnvelope, Freshness, OrderStyle
from services.decision.translator import IntentTranslator
from services.exec.models import PlacedOrder
from services.exec.protection_engine import (
    ProtectionEngine,
    ProtectiveOrderState,
    close_action_for_side,
)
from services.exec.safety_kernel import SafetyKernel


class _MS:
    async def get(self, symbol):
        return {
            "bid_px": 99.9,
            "ask_px": 100.1,
            "mid_px": 100.0,
            "last_px": 100.0,
            "tick_size": 0.01,
        }


class _Broker:
    def __init__(self):
        self.place_trailing_stop = AsyncMock(
            return_value=PlacedOrder(
                order_id=10,
                broker_order_id="10",
                order_ref="protect:trail",
                symbol="TQQQ",
                side="SELL",
                order_type="TRAIL",
                qty=3,
                trail_percent=1.5,
            )
        )
        self.place_trailing_stop_limit = AsyncMock(
            return_value=PlacedOrder(
                order_id=11,
                broker_order_id="11",
                order_ref="protect:trail-limit",
                symbol="TQQQ",
                side="SELL",
                order_type="TRAIL_LIMIT",
                qty=3,
                trail_amount=0.35,
                lmt_price_offset=0.05,
            )
        )


@pytest.mark.asyncio
async def test_translator_preserves_native_trail_order_style():
    translator = IntentTranslator({"respect_order_style": True}, _MS(), None)
    env = DecisionEnvelope(
        decision_id="d-trail",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-3,
        freshness=Freshness(),
        order_style=OrderStyle(
            kind="TRAIL",
            trail_percent=1.5,
            trail_stop_px=102.123,
            tif="DAY",
        ),
        extras={"close_only": True, "force_order_style": True},
    )

    plan = await translator.translate(env)

    assert plan.order_type == "TRAIL"
    assert plan.side == "SELL"
    assert plan.trail_percent == pytest.approx(1.5)
    assert plan.trail_stop_px == pytest.approx(102.12)


@pytest.mark.asyncio
async def test_safety_kernel_routes_trail_to_broker_and_checks_close_only_qty():
    broker = _Broker()
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)

    placed = await kernel.place_order(
        symbol="TQQQ",
        side="SELL",
        qty=3,
        order_type="TRAIL",
        trail_percent=1.5,
        trail_stop_px=102.12,
        order_ref="protect:trail",
        close_only=True,
        reduce_only=True,
        position_qty_hint=3,
    )

    assert placed is not None
    broker.place_trailing_stop.assert_awaited_once()
    kwargs = broker.place_trailing_stop.await_args.kwargs
    assert kwargs["trail_percent"] == pytest.approx(1.5)
    assert kwargs["trail_stop_px"] == pytest.approx(102.12)


@pytest.mark.asyncio
async def test_safety_kernel_rejects_long_protection_qty_over_position():
    broker = _Broker()
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)

    placed = await kernel.place_order(
        symbol="TQQQ",
        side="SELL",
        qty=4,
        order_type="TRAIL",
        trail_percent=1.5,
        order_ref="protect:trail",
        close_only=True,
        reduce_only=True,
        position_qty_hint=3,
    )

    assert placed is None
    broker.place_trailing_stop.assert_not_awaited()


def test_protection_engine_enforces_stop_monotonicity():
    with pytest.raises(ValueError, match="long protective stop cannot move down"):
        ProtectionEngine.validate_replace(
            side="LONG",
            action="SELL",
            qty=Decimal("3"),
            position_qty=Decimal("3"),
            old_stop_px=100.0,
            new_stop_px=99.5,
        )

    ProtectionEngine.validate_replace(
        side="SHORT",
        action=close_action_for_side("SHORT"),
        qty=Decimal("3"),
        position_qty=Decimal("-3"),
        old_stop_px=100.0,
        new_stop_px=99.5,
    )


def test_trail_limit_watchdog_requires_trigger_age():
    order = ProtectiveOrderState(
        position_id="p1",
        symbol="TQQQ",
        order_type="TRAIL_LIMIT",
        action="SELL",
        qty=Decimal("3"),
        status="Submitted",
        triggered_ts=100.0,
    )

    assert ProtectionEngine.trail_limit_needs_fallback(order=order, now_ts=102.0, timeout_sec=3.0) is False
    assert ProtectionEngine.trail_limit_needs_fallback(order=order, now_ts=103.1, timeout_sec=3.0) is True
