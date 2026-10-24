import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.exec.adapters.ib_adapter as ib_adapter_module
from services.exec.adapters.ib_adapter import IBInsyncAdapter, IBHandleEpochMismatch


def _mk_trade(
    *,
    order_id: int,
    status: str,
    symbol: str = "TQQQ",
    order_ref: str = "ref-1",
    oca_group: str = "",
    perm_id: int = 123,
):
    order = SimpleNamespace(
        orderId=order_id,
        orderRef=order_ref,
        action="SELL",
        orderType="STP",
        totalQuantity=10,
        auxPrice=1.0,
        tif="DAY",
        ocaGroup=oca_group,
        permId=perm_id,
    )
    contract = SimpleNamespace(symbol=symbol)
    order_status = SimpleNamespace(status=status)
    return SimpleNamespace(order=order, contract=contract, orderStatus=order_status)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_filters_pending_cancel_absent_on_fresh_probe():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [_mk_trade(order_id=36, status="PendingCancel")],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    orders = await adapter.open_orders(force_refresh=True)

    assert orders == []
    adapter.probe_order_open_fresh.assert_awaited_once_with(36)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_keeps_pending_cancel_when_fresh_probe_still_finds_it():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [_mk_trade(order_id=36, status="PendingCancel")],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": True, "status": "PendingCancel"})

    orders = await adapter.open_orders(force_refresh=True)

    assert len(orders) == 1
    assert orders[0].order_id == 36
    adapter.probe_order_open_fresh.assert_awaited_once_with(36)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_filters_pending_submit_absent_on_fresh_probe():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [_mk_trade(order_id=52, status="PendingSubmit")],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    orders = await adapter.open_orders(force_refresh=True)

    assert orders == []
    adapter.probe_order_open_fresh.assert_awaited_once_with(52)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_keeps_local_protective_pending_submit_when_fresh_probe_lags():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [
            _mk_trade(
                order_id=53,
                status="PendingSubmit",
                symbol="SOXS",
                order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:test",
                oca_group="OCO_TEST",
            )
        ],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    orders = await adapter.open_orders(force_refresh=True)

    assert len(orders) == 1
    assert orders[0].order_id == 53
    adapter.probe_order_open_fresh.assert_awaited_once_with(53)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_drops_stale_local_protective_pending_submit(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [
            _mk_trade(
                order_id=54,
                status="PendingSubmit",
                symbol="SOXS",
                order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:test",
                oca_group="OCO_TEST",
                perm_id=0,
            )
        ],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})
    monkeypatch.setattr(ib_adapter_module, "_now_us", lambda: 1_000_000)

    orders = await adapter.open_orders(force_refresh=True)

    assert orders == []
    adapter.probe_order_open_fresh.assert_awaited_once_with(54)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_filters_submitted_absent_on_fresh_probe():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [_mk_trade(order_id=78, status="Submitted")],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    orders = await adapter.open_orders(force_refresh=True)

    assert orders == []
    adapter.probe_order_open_fresh.assert_awaited_once_with(78)


@pytest.mark.asyncio
async def test_open_orders_force_refresh_keeps_submitted_when_fresh_probe_still_finds_it():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [_mk_trade(order_id=79, status="Submitted")],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": True, "status": "Submitted"})

    orders = await adapter.open_orders(force_refresh=True)

    assert len(orders) == 1
    assert orders[0].order_id == 79
    adapter.probe_order_open_fresh.assert_awaited_once_with(79)


@pytest.mark.asyncio
async def test_await_trade_identity_waits_for_non_zero_perm_id():
    adapter = IBInsyncAdapter()
    trade = _mk_trade(order_id=55, status="Submitted", order_ref="ref-55")
    trade.order.permId = 0

    async def _later_set_perm():
        await asyncio.sleep(0.02)
        trade.order.permId = 456789

    task = asyncio.create_task(_later_set_perm())
    try:
        order_ref, perm_id, status = await adapter._await_trade_identity(
            trade,
            requested_order_ref="ref-55",
            timeout_sec=0.2,
            poll_sec=0.01,
        )
    finally:
        await task

    assert order_ref == "ref-55"
    assert perm_id == 456789
    assert status == "Submitted"


@pytest.mark.asyncio
async def test_await_trade_identity_returns_terminal_status_without_perm_id():
    adapter = IBInsyncAdapter()
    trade = _mk_trade(order_id=56, status="Submitted", order_ref="ref-56")
    trade.order.permId = 0

    async def _later_cancel():
        await asyncio.sleep(0.02)
        trade.orderStatus.status = "Cancelled"

    task = asyncio.create_task(_later_cancel())
    try:
        order_ref, perm_id, status = await adapter._await_trade_identity(
            trade,
            requested_order_ref="ref-56",
            timeout_sec=0.2,
            poll_sec=0.01,
        )
    finally:
        await task

    assert order_ref == "ref-56"
    assert perm_id is None
    assert status == "Cancelled"


def test_require_epoch_raises_on_stale_epoch(monkeypatch):
    adapter = IBInsyncAdapter()
    monkeypatch.setattr(adapter, "_current_epoch", lambda: 10)

    with pytest.raises(IBHandleEpochMismatch):
        adapter._require_epoch(9, operation="place_order")


@pytest.mark.asyncio
async def test_place_market_rejects_stale_epoch_before_broker_call(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    monkeypatch.setattr(adapter, "_current_epoch", lambda: 10)

    contract = SimpleNamespace(secType="STK", symbol="QQQ")
    monkeypatch.setattr(adapter, "_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(adapter, "_normalize_quantity", lambda _c, qty: qty)
    adapter.ib = SimpleNamespace(placeOrder=lambda *_args, **_kwargs: pytest.fail("placeOrder should not be called"))

    with pytest.raises(IBHandleEpochMismatch):
        await adapter.place_market("QQQ", "BUY", 1, "DAY", "ref-epoch", expected_epoch=9)


@pytest.mark.asyncio
async def test_place_raises_when_pending_submit_never_reaches_broker():
    class _OrderStub(SimpleNamespace):
        account = ""

    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=77, status="PendingSubmit", order_ref="ref-77")
    trade.order.permId = 0
    trade.order = _OrderStub(**trade.order.__dict__)
    adapter.ib = SimpleNamespace(placeOrder=lambda contract, order: trade)
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    with pytest.raises(RuntimeError, match="ib_place_unconfirmed_open_status:77:PendingSubmit:not_found"):
        await adapter._place(
            SimpleNamespace(symbol="SOXL"),
            trade.order,
            "ref-77",
            None,
            symbol_override="SOXL",
        )


@pytest.mark.asyncio
async def test_place_raises_when_identity_timeout_never_reaches_broker():
    class _OrderStub(SimpleNamespace):
        account = ""

    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=79, status="", order_ref="ref-79")
    trade.order.permId = 0
    trade.order = _OrderStub(**trade.order.__dict__)
    adapter.ib = SimpleNamespace(placeOrder=lambda contract, order: trade)
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    with pytest.raises(RuntimeError, match="ib_place_unconfirmed_open_status:79:unknown:not_found"):
        await adapter._place(
            SimpleNamespace(symbol="SOXL"),
            trade.order,
            "ref-79",
            None,
            symbol_override="SOXL",
        )


@pytest.mark.asyncio
async def test_place_accepts_pending_submit_when_fresh_probe_confirms_broker_visibility():
    class _OrderStub(SimpleNamespace):
        account = ""

    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=78, status="PendingSubmit", order_ref="ref-78")
    trade.order.permId = 0
    trade.order = _OrderStub(**trade.order.__dict__)
    adapter.ib = SimpleNamespace(placeOrder=lambda contract, order: trade)
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": True, "status": "Submitted"})

    placed = await adapter._place(
        SimpleNamespace(symbol="SOXL"),
        trade.order,
        "ref-78",
        None,
        symbol_override="SOXL",
    )

    assert placed.order_id == 78
    assert placed.status == "Submitted"


@pytest.mark.asyncio
async def test_place_accepts_locally_submitted_order_when_fresh_probe_lags():
    class _OrderStub(SimpleNamespace):
        account = ""

    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=81, status="PendingSubmit", order_ref="ref-81")
    trade.order.permId = 0
    trade.order = _OrderStub(**trade.order.__dict__)

    refresh_count = {"value": 0}

    async def _refresh_open_orders():
        refresh_count["value"] += 1
        trade.orderStatus.status = "Submitted"

    adapter.ib = SimpleNamespace(
        placeOrder=lambda contract, order: trade,
        reqAllOpenOrdersAsync=AsyncMock(side_effect=_refresh_open_orders),
        openTrades=lambda: [trade] if refresh_count["value"] > 0 else [],
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    placed = await adapter._place(
        SimpleNamespace(symbol="SOXL"),
        trade.order,
        "ref-81",
        None,
        symbol_override="SOXL",
    )

    assert placed.order_id == 81
    assert placed.status == "Submitted"


@pytest.mark.asyncio
async def test_place_accepts_local_protective_pending_submit_when_fresh_probe_lags():
    class _OrderStub(SimpleNamespace):
        account = ""

    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(
        order_id=82,
        status="PendingSubmit",
        symbol="SOXS",
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:test",
        oca_group="OCO_TEST",
    )
    trade.order.permId = 0
    trade.order = _OrderStub(**trade.order.__dict__)

    refresh_count = {"value": 0}

    async def _refresh_open_orders():
        refresh_count["value"] += 1

    adapter.ib = SimpleNamespace(
        placeOrder=lambda contract, order: trade,
        reqAllOpenOrdersAsync=AsyncMock(side_effect=_refresh_open_orders),
        openTrades=lambda: [trade] if refresh_count["value"] > 0 else [],
    )
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "error": None})

    placed = await adapter._place(
        SimpleNamespace(symbol="SOXS"),
        trade.order,
        "sl:stocks_kernel_shadow_smh_001:SOXS:test",
        "OCO_TEST",
        symbol_override="SOXS",
    )

    assert placed.order_id == 82
    assert placed.status == "PendingSubmit"
    adapter.probe_order_open_fresh.assert_not_awaited()


def test_normalize_quantity_snaps_near_integer_for_stock():
    adapter = IBInsyncAdapter()
    contract = SimpleNamespace(secType="STK")

    qty = adapter._normalize_quantity(contract, 252.99999999999997)

    assert qty == 253.0


def test_normalize_quantity_rejects_fractional_stock_qty():
    adapter = IBInsyncAdapter()
    contract = SimpleNamespace(secType="STK")

    with pytest.raises(RuntimeError, match="non_integral_qty_not_allowed"):
        adapter._normalize_quantity(contract, 252.5)


@pytest.mark.asyncio
async def test_cancel_with_owner_uses_targeted_owner_client_path_when_foreign():
    adapter = IBInsyncAdapter(client_id=207)
    adapter._connect = AsyncMock(return_value=None)
    foreign_trade = _mk_trade(order_id=88, status="PreSubmitted", order_ref="ref-88")
    foreign_trade.order.clientId = 1259
    adapter._trades = {88: foreign_trade}
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [foreign_trade],
        reqAllOpenOrdersAsync=AsyncMock(return_value=None),
        cancelOrder=lambda order: None,
        isConnected=lambda: True,
    )
    adapter._cancel_via_owner_client = AsyncMock(return_value=True)

    ok = await adapter.cancel_with_owner(88, owner_client_id=1259, perm_id=456, order_ref="ref-88")

    assert ok is True
    adapter._cancel_via_owner_client.assert_awaited_once_with(
        order_id=88,
        owner_client_id=1259,
        perm_id=456,
        order_ref="ref-88",
    )


@pytest.mark.asyncio
async def test_modify_waits_until_trade_leaves_pending_submit():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=91, status="PendingSubmit", order_ref="ref-91")
    adapter._trades = {91: trade}
    place_order_calls = []

    def _place_order(contract, order):
        place_order_calls.append((contract, order.totalQuantity))

    adapter.ib = SimpleNamespace(
        openTrades=lambda: [trade],
        placeOrder=_place_order,
    )

    async def _later_submit():
        await asyncio.sleep(0.02)
        trade.orderStatus.status = "Submitted"

    task = asyncio.create_task(_later_submit())
    try:
        ok = await adapter.modify(91, qty=12)
    finally:
        await task

    assert ok is True
    assert trade.order.totalQuantity == 12.0
    assert place_order_calls == [(trade.contract, 12.0)]


@pytest.mark.asyncio
async def test_modify_returns_false_when_trade_never_leaves_pending_submit():
    adapter = IBInsyncAdapter()
    adapter._connect = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=92, status="PendingSubmit", order_ref="ref-92")
    adapter._trades = {92: trade}
    adapter._await_trade_modifiable = AsyncMock(return_value=False)
    adapter.ib = SimpleNamespace(
        openTrades=lambda: [trade],
        placeOrder=lambda contract, order: (_ for _ in ()).throw(AssertionError("should not modify pending order")),
    )

    ok = await adapter.modify(92, qty=12)

    assert ok is False
    assert trade.order.totalQuantity == 10


@pytest.mark.asyncio
async def test_normalize_price_fields_rounds_to_contract_min_tick():
    adapter = IBInsyncAdapter()
    adapter.ib = SimpleNamespace(
        reqContractDetailsAsync=AsyncMock(return_value=[SimpleNamespace(minTick=0.05)])
    )
    contract = SimpleNamespace(conId=73340487, symbol="SOXS", localSymbol="SOXS")

    limit_px, stop_px = await adapter._normalize_price_fields(
        contract,
        limit_px=39.283,
        stop_px=39.287,
    )

    assert limit_px == pytest.approx(39.30)
    assert stop_px == pytest.approx(39.30)


@pytest.mark.asyncio
async def test_place_stop_limit_uses_tick_normalized_prices():
    adapter = IBInsyncAdapter()
    contract = SimpleNamespace(conId=73340487, secType="STK", symbol="SOXS", localSymbol="SOXS")
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[contract]),
        reqContractDetailsAsync=AsyncMock(return_value=[SimpleNamespace(minTick=0.05)]),
    )
    adapter._place = AsyncMock(return_value=SimpleNamespace(order_id=1))

    await adapter.place_stop_limit(
        "SOXS",
        "BUY",
        254.0,
        39.287,
        38.123,
        "DAY",
        "sl:test",
    )

    placed_order = adapter._place.await_args.args[1]
    assert placed_order.auxPrice == pytest.approx(39.30)
    assert placed_order.lmtPrice == pytest.approx(38.10)


@pytest.mark.asyncio
async def test_place_trailing_stop_builds_ib_trail_order():
    adapter = IBInsyncAdapter()
    contract = SimpleNamespace(conId=73340487, secType="STK", symbol="SOXS", localSymbol="SOXS")
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[contract]),
        reqContractDetailsAsync=AsyncMock(return_value=[SimpleNamespace(minTick=0.05)]),
    )
    adapter._place = AsyncMock(return_value=SimpleNamespace(order_id=1))

    await adapter.place_trailing_stop(
        "SOXS",
        "SELL",
        100.0,
        "DAY",
        "trail:test",
        trail_percent=1.5,
        trail_stop_px=42.023,
    )

    placed_order = adapter._place.await_args.args[1]
    assert placed_order.orderType == "TRAIL"
    assert placed_order.trailingPercent == pytest.approx(1.5)
    assert placed_order.trailStopPrice == pytest.approx(42.00)


@pytest.mark.asyncio
async def test_place_trailing_stop_limit_builds_ib_trail_limit_order():
    adapter = IBInsyncAdapter()
    contract = SimpleNamespace(conId=73340487, secType="STK", symbol="SOXS", localSymbol="SOXS")
    adapter._connect = AsyncMock(return_value=None)
    adapter.ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[contract]),
        reqContractDetailsAsync=AsyncMock(return_value=[SimpleNamespace(minTick=0.05)]),
    )
    adapter._place = AsyncMock(return_value=SimpleNamespace(order_id=1))

    await adapter.place_trailing_stop_limit(
        "SOXS",
        "SELL",
        100.0,
        "DAY",
        "trail-limit:test",
        trail_amount=0.37,
        trail_stop_px=42.023,
        lmt_price_offset=0.08,
    )

    placed_order = adapter._place.await_args.args[1]
    assert placed_order.orderType == "TRAIL LIMIT"
    assert placed_order.auxPrice == pytest.approx(0.37)
    assert placed_order.trailStopPrice == pytest.approx(42.00)
    assert placed_order.lmtPriceOffset == pytest.approx(0.10)
