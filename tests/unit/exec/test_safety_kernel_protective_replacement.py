from __future__ import annotations

import pytest

from services.exec.models import PlacedOrder
from services.exec.safety_kernel import SafetyKernel


class _ReplaceBroker:
    def __init__(self) -> None:
        self.modified: list[dict] = []
        self.replaced: list[dict] = []

    async def modify_stop_order(
        self,
        order_id: int,
        *,
        stop_px: float | None = None,
        qty: float | None = None,
        expected_epoch: int | None = None,
    ) -> bool:
        self.modified.append(
            {
                "order_id": order_id,
                "stop_px": stop_px,
                "qty": qty,
                "expected_epoch": expected_epoch,
            }
        )
        return True

    async def replace_protective_order_atomicish(self, **kwargs) -> PlacedOrder:
        self.replaced.append(dict(kwargs))
        return PlacedOrder(
            order_id=222,
            order_ref=str(kwargs.get("order_ref") or ""),
            symbol=str(kwargs.get("symbol") or ""),
            side=str(kwargs.get("side") or ""),
            order_type=str(kwargs.get("order_type") or ""),
            qty=float(kwargs.get("qty") or 0.0),
            trail_percent=kwargs.get("trail_percent"),
            trail_stop_px=kwargs.get("trail_stop_px"),
        )


@pytest.mark.asyncio
async def test_protective_replacement_modifies_same_type_stop_in_place():
    broker = _ReplaceBroker()
    kernel = SafetyKernel(broker)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:STOP:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="STP",
        qty=2.0,
        stop_px=48.5,
    )

    order = await kernel.replace_protective_order_atomicish(
        old_order_id=111,
        symbol="TQQQ",
        side="SELL",
        qty=2.0,
        order_type="STP",
        stop_px=50.0,
        order_ref="iswv2:s1:TQQQ:STOP:TRG:x:2",
        position_qty_hint=2.0,
        previous_stop_px=48.5,
    )

    assert order is kernel.orders[111]
    assert order.stop_px == 50.0
    assert broker.modified == [{"order_id": 111, "stop_px": 50.0, "qty": 2.0, "expected_epoch": None}]
    assert broker.replaced == []


@pytest.mark.asyncio
async def test_protective_replacement_uses_atomicish_replace_for_cross_type_trail():
    broker = _ReplaceBroker()
    kernel = SafetyKernel(broker)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:STOP:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="STP",
        qty=2.0,
        stop_px=48.5,
    )

    order = await kernel.replace_protective_order_atomicish(
        old_order_id=111,
        symbol="TQQQ",
        side="SELL",
        qty=2.0,
        order_type="TRAIL",
        trail_percent=1.5,
        trail_stop_px=50.0,
        order_ref="iswv2:s1:TQQQ:TRAIL:TRG:x:1",
        position_qty_hint=2.0,
        previous_stop_px=48.5,
    )

    assert order is not None
    assert order.order_id == 222
    assert 111 not in kernel.orders
    assert kernel.orders[222].order_type == "TRAIL"
    assert broker.modified == []
    assert broker.replaced[0]["old_order_id"] == 111
