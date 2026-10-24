from __future__ import annotations

from decimal import Decimal

import pytest

from services.exec.models import PlacedOrder
from services.exec.protection_engine import ProtectionSpec
from services.exec.safety_kernel import SafetyKernel


class _FamilyBroker:
    def __init__(self) -> None:
        self.cancelled: list[int] = []
        self.modified: list[dict] = []
        self.placed: list[dict] = []

    async def cancel(self, order_id: int, *, expected_epoch: int | None = None) -> None:
        self.cancelled.append(int(order_id))

    async def modify_stop_order(
        self,
        order_id: int,
        *,
        stop_px: float | None = None,
        qty: float | None = None,
        expected_epoch: int | None = None,
    ) -> bool:
        self.modified.append({"order_id": int(order_id), "stop_px": stop_px, "qty": qty})
        return True

    async def place_stop(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_px: float,
        tif: str,
        order_ref: str,
        *,
        outside_rth: bool | None = None,
        exchange_override: str | None = None,
        expected_epoch: int | None = None,
    ) -> PlacedOrder:
        self.placed.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "stop_px": stop_px,
                "order_ref": order_ref,
            }
        )
        return PlacedOrder(
            order_id=201,
            order_ref=order_ref,
            symbol=symbol,
            side=side,
            order_type="STP",
            qty=qty,
            stop_px=stop_px,
            tif=tif,
        )


def _protective(order_id: int, *, qty: float = 4.0, kind: str = "STOP", ts_us: int = 1) -> PlacedOrder:
    order_type = "TRAIL" if kind == "TRAIL" else "STP"
    return PlacedOrder(
        order_id=order_id,
        order_ref=f"iswv2:s1:TQQQ:{kind}:TRG:trigger-1:1",
        symbol="TQQQ",
        side="SELL",
        order_type=order_type,
        qty=qty,
        stop_px=48.5 if order_type == "STP" else None,
        trail_percent=1.5 if order_type == "TRAIL" else None,
        trail_stop_px=50.0 if order_type == "TRAIL" else None,
        status="SUBMITTED",
        ts_us=ts_us,
    )


@pytest.mark.asyncio
async def test_reconcile_cancels_duplicate_protection_family_keeps_newest() -> None:
    broker = _FamilyBroker()
    kernel = SafetyKernel(broker)
    kernel.orders[101] = _protective(101, kind="STOP", ts_us=10)
    kernel.orders[102] = _protective(102, kind="TRAIL", ts_us=20)

    summary = await kernel.reconcile_protective_order_families_once(position_qty_by_symbol={"TQQQ": 4.0})

    assert summary["cancelled"] == 1
    assert broker.cancelled == [101]
    assert 101 not in kernel.orders
    assert 102 in kernel.orders


@pytest.mark.asyncio
async def test_reconcile_resizes_protection_when_family_target_qty_changes() -> None:
    broker = _FamilyBroker()
    kernel = SafetyKernel(broker)
    order = _protective(101, kind="STOP", qty=4.0, ts_us=10)
    kernel.orders[101] = order
    family = kernel._protection_family_key(order)

    summary = await kernel.reconcile_protective_order_families_once(target_qty_by_family={family: 2.0})

    assert summary["resized"] == 1
    assert broker.modified == [{"order_id": 101, "stop_px": None, "qty": 2.0}]
    assert kernel.orders[101].qty == 2.0


@pytest.mark.asyncio
async def test_reconcile_cancels_residual_protection_when_symbol_position_flat() -> None:
    broker = _FamilyBroker()
    kernel = SafetyKernel(broker)
    kernel.orders[101] = _protective(101, kind="STOP", qty=4.0, ts_us=10)

    summary = await kernel.reconcile_protective_order_families_once(position_qty_by_symbol={"TQQQ": 0.0})

    assert summary["cancelled"] == 1
    assert broker.cancelled == [101]
    assert 101 not in kernel.orders


@pytest.mark.asyncio
async def test_reconcile_can_skip_broker_symbol_qty_to_avoid_cross_strategy_shrink() -> None:
    broker = _FamilyBroker()
    kernel = SafetyKernel(broker)
    kernel.orders[101] = _protective(101, kind="STOP", qty=4.0, ts_us=10)

    summary = await kernel.reconcile_protective_order_families_once(use_broker_symbol_qty=False)

    assert summary["resized"] == 0
    assert broker.modified == []
    assert kernel.orders[101].qty == 4.0


@pytest.mark.asyncio
async def test_reconcile_rebuilds_missing_protection_from_desired_spec() -> None:
    broker = _FamilyBroker()
    kernel = SafetyKernel(broker)
    spec = ProtectionSpec(
        symbol="TQQQ",
        position_id="trigger-1",
        side="LONG",
        qty=Decimal("2"),
        entry_px=50.0,
        current_px=51.0,
        initial_stop_px=48.5,
        initial_risk_per_share=1.5,
        mode="RATCHET_STP",
        desired_stop_px=50.25,
    )

    summary = await kernel.reconcile_protective_order_families_once(
        desired_specs_by_family={"iswv2:s1:TQQQ:PROTECT:TRG:trigger-1": spec}
    )

    assert summary["rebuilt"] == 1
    assert broker.placed == [
        {
            "symbol": "TQQQ",
            "side": "SELL",
            "qty": 2.0,
            "stop_px": 50.25,
            "order_ref": "protect:iswv2:s1:TQQQ:PROTECT:TRG:trigger-1",
        }
    ]
    assert kernel.orders[201].order_type == "STP"


@pytest.mark.asyncio
async def test_reconcile_recognizes_rebuilt_protect_family_ref_without_rebuilding_again() -> None:
    broker = _FamilyBroker()
    kernel = SafetyKernel(broker)
    family = "iswv2:s1:TQQQ:PROTECT:TRG:trigger-1"
    kernel.orders[201] = PlacedOrder(
        order_id=201,
        order_ref=f"protect:{family}",
        symbol="TQQQ",
        side="SELL",
        order_type="STP",
        qty=2.0,
        stop_px=50.25,
        status="SUBMITTED",
    )
    spec = ProtectionSpec(
        symbol="TQQQ",
        position_id="trigger-1",
        side="LONG",
        qty=Decimal("2"),
        entry_px=50.0,
        current_px=51.0,
        initial_stop_px=48.5,
        initial_risk_per_share=1.5,
        mode="RATCHET_STP",
        desired_stop_px=50.25,
    )

    summary = await kernel.reconcile_protective_order_families_once(desired_specs_by_family={family: spec})

    assert summary["rebuilt"] == 0
    assert broker.placed == []
    assert kernel._protection_family_key(kernel.orders[201]) == family
