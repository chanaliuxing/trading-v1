import pytest
from types import SimpleNamespace

from services.strategy.engine import RealEngine
from services.orders.truth import EffectiveOpenOrdersSnapshot


class _StubPG:
    async def fetchall(self, query: str, strategy_id: str):
        assert "FROM core.core_order" in query
        assert strategy_id == "qqq_intraday_001"
        return [
            {
                "order_id": "oid-1",
                "strategy_id": strategy_id,
                "symbol": "TQQQ",
                "state": "SUBMITTED",
                "order_type": "STP",
                "order_ref": "qqqv1:qqq_intraday_001:TQQQ:STOP:1",
                "note": "protective_stop",
                "stop_px": 47.28,
                "limit_px": None,
                "broker_order_id": "6",
                "ib_perm_id": 123,
                "parent_order_id": None,
                "oca_group": None,
                "last_error": None,
                "updated_at": None,
            }
        ]


@pytest.mark.asyncio
async def test_open_orders_view_includes_order_ref_and_type():
    fake_engine = SimpleNamespace(pg=_StubPG())
    orders = await RealEngine.open_orders_view(fake_engine, "qqq_intraday_001")

    assert len(orders) == 1
    o = orders[0]
    assert o.symbol == "TQQQ"
    assert o.state == "SUBMITTED"
    assert o.order_type == "STP"
    assert o.order_ref.endswith(":STOP:1")
    assert o.note == "protective_stop"
    assert o.stop_px == 47.28


@pytest.mark.asyncio
async def test_open_orders_view_delegates_to_order_truth_service_when_present():
    expected_order = SimpleNamespace(
        order_id="oid-2",
        strategy_id="qqq_intraday_001",
        symbol="SQQQ",
        state="SUBMITTED",
    )

    class _TruthService:
        async def effective_open_orders(self, *, strategy_id: str, force_refresh: bool = False):
            assert strategy_id == "qqq_intraday_001"
            assert force_refresh is False
            return EffectiveOpenOrdersSnapshot(
                strategy_id=strategy_id,
                source="broker_plus_local_grace",
                orders=[expected_order],
                broker_available=True,
                broker_count=1,
                db_count=0,
            )

    fake_engine = SimpleNamespace(pg=_StubPG(), order_truth_service=_TruthService())
    orders = await RealEngine.open_orders_view(fake_engine, "qqq_intraday_001")

    assert orders == [expected_order]


@pytest.mark.asyncio
async def test_position_view_delegates_to_order_truth_service_when_present():
    expected_position = SimpleNamespace(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        pos_qty=88.0,
    )

    class _TruthService:
        async def effective_position(self, *, strategy_id: str, symbol: str):
            assert strategy_id == "qqq_intraday_001"
            assert symbol == "TQQQ"
            return SimpleNamespace(
                strategy_id=strategy_id,
                symbol=symbol,
                source="broker_primary",
                position=expected_position,
                broker_available=True,
                broker_qty=88.0,
                db_qty=126.0,
            )

    fake_engine = SimpleNamespace(pg=_StubPG(), order_truth_service=_TruthService())
    position = await RealEngine.position_view(fake_engine, "qqq_intraday_001", "TQQQ")

    assert position is expected_position
