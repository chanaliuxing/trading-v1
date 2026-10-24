from __future__ import annotations

import pytest

from services.exec.models import PlacedOrder
from services.exec.safety_kernel import SafetyKernel


class _FakeBroker:
    def __init__(self) -> None:
        self.cancelled: list[tuple[int, int | None]] = []
        self.emergency_orders: list[dict] = []

    async def cancel(self, order_id: int, *, expected_epoch: int | None = None) -> None:
        self.cancelled.append((int(order_id), expected_epoch))

    async def emergency_exit(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        *,
        limit_px: float | None = None,
        outside_rth: bool | None = None,
        exchange_override: str | None = None,
        expected_epoch: int | None = None,
    ) -> PlacedOrder:
        self.emergency_orders.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "tif": tif,
                "order_ref": order_ref,
                "limit_px": limit_px,
                "expected_epoch": expected_epoch,
            }
        )
        return PlacedOrder(
            order_id=222,
            order_ref=order_ref,
            symbol=symbol,
            side=side,
            order_type="LMT" if limit_px is not None else "MKT",
            qty=qty,
            limit_px=limit_px,
            tif=tif,
        )


class _CancelFailBroker(_FakeBroker):
    async def cancel(self, order_id: int, *, expected_epoch: int | None = None) -> None:
        raise RuntimeError("cancel failed")


class _EmergencyFailBroker(_FakeBroker):
    async def emergency_exit(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        *,
        limit_px: float | None = None,
        outside_rth: bool | None = None,
        exchange_override: str | None = None,
        expected_epoch: int | None = None,
    ) -> PlacedOrder:
        raise RuntimeError("emergency failed")


class _EmergencyNoneBroker(_FakeBroker):
    async def emergency_exit(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str,
        order_ref: str,
        *,
        limit_px: float | None = None,
        outside_rth: bool | None = None,
        exchange_override: str | None = None,
        expected_epoch: int | None = None,
    ) -> PlacedOrder | None:
        self.emergency_orders.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "tif": tif,
                "order_ref": order_ref,
                "limit_px": limit_px,
                "expected_epoch": expected_epoch,
            }
        )
        return None


class _NoEmergencyBroker:
    def __init__(self) -> None:
        self.cancelled: list[int] = []

    async def cancel(self, order_id: int, *, expected_epoch: int | None = None) -> None:
        self.cancelled.append(int(order_id))


@pytest.mark.asyncio
async def test_trail_limit_watchdog_escalates_triggered_unfilled_order_to_marketable_limit():
    broker = _FakeBroker()
    kernel = SafetyKernel(
        broker,
        trail_limit_unfilled_timeout_sec=3.0,
        trail_limit_fallback="MARKETABLE_LMT",
        trail_limit_emergency_slippage_bps=20.0,
    )
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Submitted",
    )

    first = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.95, "bid": 49.9, "ask": 49.92}},
        now_ts=100.0,
    )
    second = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.9, "bid": 49.88, "ask": 49.9}},
        now_ts=103.1,
    )

    assert first == 0
    assert second == 1
    assert broker.cancelled == [(111, 0)]
    assert broker.emergency_orders[0]["limit_px"] == 49.78
    assert 111 not in kernel.orders
    assert kernel.orders[222].order_type == "LMT"


@pytest.mark.asyncio
async def test_trail_limit_watchdog_ignores_untriggered_order():
    broker = _FakeBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Submitted",
    )

    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 50.25, "bid": 50.24, "ask": 50.26}},
        now_ts=200.0,
    )

    assert escalated == 0
    assert broker.cancelled == []
    assert broker.emergency_orders == []
    assert 111 in kernel.orders


@pytest.mark.asyncio
async def test_trail_limit_watchdog_does_not_cancel_without_emergency_capability():
    broker = _NoEmergencyBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Submitted",
    )

    await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.95, "bid": 49.9, "ask": 49.92}},
        now_ts=100.0,
    )
    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.9, "bid": 49.88, "ask": 49.9}},
        now_ts=103.1,
    )

    assert escalated == 0
    assert broker.cancelled == []
    assert 111 in kernel.orders


@pytest.mark.asyncio
async def test_trail_limit_watchdog_does_not_submit_emergency_when_cancel_fails():
    broker = _CancelFailBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Submitted",
    )

    await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.95, "bid": 49.9, "ask": 49.92}},
        now_ts=100.0,
    )
    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.9, "bid": 49.88, "ask": 49.9}},
        now_ts=103.1,
    )

    assert escalated == 0
    assert broker.emergency_orders == []
    assert 111 in kernel.orders


@pytest.mark.asyncio
async def test_trail_limit_watchdog_marks_cancelled_when_emergency_raises_after_cancel():
    broker = _EmergencyFailBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Submitted",
    )

    await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.95, "bid": 49.9, "ask": 49.92}},
        now_ts=100.0,
    )
    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.9, "bid": 49.88, "ask": 49.9}},
        now_ts=103.1,
    )
    retry = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.8, "bid": 49.78, "ask": 49.8}},
        now_ts=106.2,
    )

    assert escalated == 0
    assert retry == 0
    assert broker.cancelled == [(111, 0)]
    assert kernel.orders[111].status == "CANCELLED"
    assert kernel._trail_limit_triggered_ts_by_order_id == {}


@pytest.mark.asyncio
async def test_trail_limit_watchdog_marks_cancelled_when_emergency_returns_none_after_cancel():
    broker = _EmergencyNoneBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Submitted",
    )

    await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.95, "bid": 49.9, "ask": 49.92}},
        now_ts=100.0,
    )
    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.9, "bid": 49.88, "ask": 49.9}},
        now_ts=103.1,
    )

    assert escalated == 0
    assert broker.cancelled == [(111, 0)]
    assert len(broker.emergency_orders) == 1
    assert kernel.orders[111].status == "CANCELLED"
    assert kernel._trail_limit_triggered_ts_by_order_id == {}


@pytest.mark.asyncio
async def test_trail_limit_watchdog_ignores_terminal_order():
    broker = _FakeBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="Filled",
    )

    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.5, "bid": 49.48, "ask": 49.5}},
        now_ts=300.0,
    )

    assert escalated == 0
    assert broker.cancelled == []
    assert broker.emergency_orders == []
    assert 111 in kernel.orders


@pytest.mark.asyncio
async def test_trail_limit_watchdog_ignores_api_cancelled_order():
    broker = _FakeBroker()
    kernel = SafetyKernel(broker, trail_limit_unfilled_timeout_sec=3.0)
    kernel.orders[111] = PlacedOrder(
        order_id=111,
        order_ref="iswv2:s1:TQQQ:TRAIL_LIMIT:TRG:x:1",
        symbol="TQQQ",
        side="SELL",
        order_type="TRAIL_LIMIT",
        qty=2.0,
        trail_stop_px=50.0,
        lmt_price_offset=0.03,
        tif="DAY",
        status="ApiCancelled",
    )

    escalated = await kernel.watch_trail_limit_orders_once(
        market_snapshots={"TQQQ": {"last": 49.5, "bid": 49.48, "ask": 49.5}},
        now_ts=300.0,
    )

    assert escalated == 0
    assert broker.cancelled == []
    assert broker.emergency_orders == []
