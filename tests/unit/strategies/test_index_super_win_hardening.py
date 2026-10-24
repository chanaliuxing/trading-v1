from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from libs.strategy import StrategyParams
from strategies.index_super_win_strategy import IndexSuperWinStrategy


@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.strategy_id = "index_super_win_001"
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib.positions.return_value = []
    ib.openTrades.return_value = []
    ib_adapter = MagicMock()
    ib_adapter.ib = ib
    ib_adapter.account = "DU1"
    market_data_service = MagicMock()
    market_data_service.ib_adapter = ib_adapter
    ctx.engine = MagicMock()
    ctx.engine.market_data_service = market_data_service
    ctx.subscribe_features = AsyncMock()
    ctx.set_cadence = AsyncMock()
    ctx.submit_intent = AsyncMock()
    ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    return ctx


@pytest.mark.asyncio
async def test_current_exec_qty_does_not_claim_broker_when_ctx_flat(mock_ctx):
    strategy = IndexSuperWinStrategy(
        mock_ctx,
        StrategyParams(cadence_s=5, ttl_ms=2000, extras={"symbols": ["AAPL"], "dry_run": True}),
    )
    await strategy.initialize()

    strategy._broker_position_qty = AsyncMock(return_value=137.0)
    qty = await strategy._current_exec_qty_for_symbol("AAPL")

    assert qty == 0.0


@pytest.mark.asyncio
async def test_current_exec_qty_fails_safe_to_zero_on_ctx_error(mock_ctx):
    mock_ctx.position = AsyncMock(side_effect=RuntimeError("ctx down"))
    strategy = IndexSuperWinStrategy(
        mock_ctx,
        StrategyParams(cadence_s=5, ttl_ms=2000, extras={"symbols": ["AAPL"], "dry_run": True}),
    )
    await strategy.initialize()

    qty = await strategy._current_exec_qty_for_symbol("AAPL")

    assert qty == 0.0


@pytest.mark.asyncio
async def test_initialize_quarantines_unknown_open_order(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=22,
                action="BUY",
                totalQuantity=10.0,
                orderType="LMT",
                orderRef="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:abc",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    strategy = IndexSuperWinStrategy(
        mock_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "symbols": ["AAPL"],
                "dry_run": True,
                "broker_reconcile_on_init": True,
            },
        ),
    )

    await strategy.initialize()

    state = await strategy._runtime_store.get_state(strategy.strategy_id, "AAPL")
    assert state is not None
    assert state.lifecycle.value == "quarantine"


def test_extract_submit_status_handles_dict_and_object():
    dict_status = IndexSuperWinStrategy._extract_submit_status({"status": "approved", "reasons": ["ok"]})
    assert dict_status == ("APPROVED", ["ok"])

    obj = SimpleNamespace(status="deferred", reasons=["cap"])  # noqa: N806
    obj_status = IndexSuperWinStrategy._extract_submit_status(obj)
    assert obj_status == ("DEFERRED", ["cap"])
