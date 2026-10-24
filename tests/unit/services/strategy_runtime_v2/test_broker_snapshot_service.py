from types import SimpleNamespace

import pytest

from services.strategy_runtime_v2.broker_snapshot_service import BrokerSnapshotService


class _DummyPositionSource:
    async def positions(self):
        return [
            SimpleNamespace(
                account="DU1",
                contract=SimpleNamespace(symbol="AAPL"),
                position=10.0,
                avgCost=100.0,
            ),
            {
                "account": "DU1",
                "symbol": "AAPL",
                "qty": 5.0,
                "avg_px": 110.0,
            },
            SimpleNamespace(
                account="DU1",
                contract=SimpleNamespace(symbol="MSFT"),
                position=7.0,
                avgCost=200.0,
            ),
        ]


class _DummyOrderSource:
    async def open_orders(self):
        return [
            SimpleNamespace(
                order_id=11,
                strategy_id="index_super_win_001",
                symbol="AAPL",
                side="BUY",
                qty=10.0,
                order_type="MKT",
                state="Submitted",
                order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc",
            ),
            {
                "broker_order_id": "12",
                "symbol": "MSFT",
                "side": "SELL",
                "qty": 7.0,
                "order_type": "STP",
                "state": "PreSubmitted",
                "order_ref": "iswv2:index_super_win_001:MSFT:STOP:TRG:def",
            },
        ]


@pytest.mark.asyncio
async def test_read_position_aggregates_symbol_rows_and_weighted_avg():
    service = BrokerSnapshotService(position_source=_DummyPositionSource())

    snapshot = await service.read_position("AAPL")

    assert snapshot is not None
    assert snapshot.symbol == "AAPL"
    assert snapshot.qty == 15.0
    assert round(float(snapshot.avg_px or 0.0), 4) == 103.3333


@pytest.mark.asyncio
async def test_read_open_orders_normalizes_objects_and_dicts():
    service = BrokerSnapshotService(open_order_source=_DummyOrderSource())

    orders = await service.read_open_orders()

    assert len(orders) == 2
    assert orders[0].broker_order_id == "11"
    assert orders[0].symbol == "AAPL"
    assert orders[1].broker_order_id == "12"
    assert orders[1].order_type == "STP"
