from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.orders.truth import OrderTruthService


class _StubPG:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self, query: str, strategy_id: str):
        assert "FROM core.core_order" in query
        return list(self.rows)

    async def fetchone(self, query: str, strategy_id: str, symbol: str, *args):
        assert "FROM core.position_ledger_current" in query
        for row in self.rows:
            if row.get("strategy_id") == strategy_id and row.get("symbol") == symbol:
                return dict(row)
        return None


class _StubBroker:
    def __init__(self, orders=None, error: Exception | None = None):
        self.orders = list(orders or [])
        self.error = error
        self.calls = []

    async def open_orders(self, *, force_refresh: bool = False):
        self.calls.append(force_refresh)
        if self.error is not None:
            raise self.error
        return list(self.orders)

    async def position_qty(self, symbol: str):
        if self.error is not None:
            raise self.error
        return getattr(self, "qty_map", {}).get(symbol, 0.0)


def _db_row(*, age_sec: float, order_id: str = "oid-1", perm_id: int | None = None, order_ref: str = "ref-1"):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return {
        "order_id": order_id,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "state": "SUBMITTED",
        "order_type": "STP",
        "order_ref": order_ref,
        "note": "protective_stop",
        "stop_px": 47.28,
        "limit_px": None,
        "broker_order_id": "6",
        "ib_perm_id": perm_id,
        "parent_order_id": None,
        "oca_group": None,
        "last_error": None,
        "created_at": ts,
        "updated_at": ts,
    }


def _position_row(*, age_sec: float, qty: float, symbol: str = "TQQQ"):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return {
        "strategy_id": "qqq_intraday_001",
        "symbol": symbol,
        "pos_qty": qty,
        "avg_px": 48.12,
        "realized_pnl_usd": 12.5,
        "created_at": ts,
        "updated_at": ts,
    }


class _BrokerOrder:
    def __init__(self, **kwargs):
        self.order_id = kwargs.get("order_id", 6)
        self.broker_order_id = kwargs.get("broker_order_id", "6")
        self.order_ref = kwargs.get("order_ref", "ref-1")
        self.symbol = kwargs.get("symbol", "TQQQ")
        self.status = kwargs.get("status", "Submitted")
        self.order_type = kwargs.get("order_type", "STP")
        self.stop_px = kwargs.get("stop_px", 47.28)
        self.limit_px = kwargs.get("limit_px", None)
        self.oca_group = kwargs.get("oca_group", None)
        self.perm_id = kwargs.get("perm_id", None)
        self.strategy_id = kwargs.get("strategy_id", None)
        self.ts_us = kwargs.get("ts_us", int(datetime.now(timezone.utc).timestamp() * 1_000_000))


@pytest.mark.asyncio
async def test_effective_open_orders_hides_stale_db_only_rows_when_broker_flat():
    service = OrderTruthService(
        pg=_StubPG([_db_row(age_sec=600.0)]),
        broker_open_order_source=_StubBroker([]),
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_open_orders(strategy_id="qqq_intraday_001")

    assert snapshot.source == "broker_plus_local_grace"
    assert snapshot.broker_available is True
    assert snapshot.orders == []


@pytest.mark.asyncio
async def test_effective_open_orders_keeps_recent_db_row_during_broker_visibility_gap():
    service = OrderTruthService(
        pg=_StubPG([_db_row(age_sec=5.0)]),
        broker_open_order_source=_StubBroker([]),
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_open_orders(strategy_id="qqq_intraday_001")

    assert len(snapshot.orders) == 1
    assert snapshot.orders[0].order_id == "oid-1"
    assert snapshot.orders[0].state == "SUBMITTED"


@pytest.mark.asyncio
async def test_effective_open_orders_enriches_broker_order_with_db_metadata():
    service = OrderTruthService(
        pg=_StubPG([_db_row(age_sec=5.0, perm_id=123)]),
        broker_open_order_source=_StubBroker([_BrokerOrder(perm_id=123, order_ref="ref-1")]),
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_open_orders(strategy_id="qqq_intraday_001")

    assert len(snapshot.orders) == 1
    order = snapshot.orders[0]
    assert order.strategy_id == "qqq_intraday_001"
    assert order.order_ref == "ref-1"
    assert order.order_type == "STP"
    assert order.stop_px == 47.28


@pytest.mark.asyncio
async def test_effective_open_orders_falls_back_to_db_when_broker_unavailable():
    service = OrderTruthService(
        pg=_StubPG([_db_row(age_sec=600.0)]),
        broker_open_order_source=_StubBroker(error=RuntimeError("ib down")),
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_open_orders(strategy_id="qqq_intraday_001")

    assert snapshot.source == "db_fallback"
    assert snapshot.broker_available is False
    assert len(snapshot.orders) == 1


@pytest.mark.asyncio
async def test_effective_open_orders_prefers_fresh_runtime_reconciled_truth():
    service = OrderTruthService(
        pg=_StubPG([_db_row(age_sec=600.0)]),
        broker_open_order_source=_StubBroker(error=RuntimeError("ib down")),
        pending_grace_sec=30.0,
        runtime_snapshot_ttl_sec=90.0,
    )
    service.publish_reconciled_open_orders(
        db_orders=[],
        ib_orders=[],
        published_at=datetime.now(timezone.utc),
    )

    snapshot = await service.effective_open_orders(strategy_id="qqq_intraday_001")

    assert snapshot.source == "runtime_reconciled_truth"
    assert snapshot.broker_available is True
    assert snapshot.orders == []


@pytest.mark.asyncio
async def test_effective_open_orders_uses_reconciled_truth_with_broker_match():
    row = _db_row(age_sec=5.0, perm_id=123, order_id="oid-published", order_ref="ref-published")
    service = OrderTruthService(
        pg=_StubPG([row]),
        broker_open_order_source=_StubBroker([]),
        pending_grace_sec=30.0,
        runtime_snapshot_ttl_sec=90.0,
    )
    service.publish_reconciled_open_orders(
        db_orders=[row],
        ib_orders=[_BrokerOrder(perm_id=123, order_ref="ref-published")],
        published_at=datetime.now(timezone.utc),
    )

    snapshot = await service.effective_open_orders(strategy_id="qqq_intraday_001")

    assert snapshot.source == "runtime_reconciled_truth"
    assert len(snapshot.orders) == 1
    assert snapshot.orders[0].strategy_id == "qqq_intraday_001"
    assert snapshot.orders[0].order_ref == "ref-published"
    assert snapshot.orders[0].stop_px == 47.28


@pytest.mark.asyncio
async def test_effective_position_hides_stale_db_only_qty_when_broker_flat():
    broker = _StubBroker([])
    broker.qty_map = {"TQQQ": 0.0}
    service = OrderTruthService(
        pg=_StubPG([_position_row(age_sec=600.0, qty=126.0)]),
        broker_open_order_source=broker,
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_position(strategy_id="qqq_intraday_001", symbol="TQQQ")

    assert snapshot.source == "broker_primary"
    assert snapshot.position.pos_qty == 0.0
    assert snapshot.position.avg_px is None


@pytest.mark.asyncio
async def test_effective_position_keeps_recent_db_qty_during_broker_visibility_gap():
    broker = _StubBroker([])
    broker.qty_map = {"TQQQ": 0.0}
    service = OrderTruthService(
        pg=_StubPG([_position_row(age_sec=5.0, qty=126.0)]),
        broker_open_order_source=broker,
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_position(strategy_id="qqq_intraday_001", symbol="TQQQ")

    assert snapshot.source == "db_recent_grace"
    assert snapshot.position.pos_qty == 126.0


@pytest.mark.asyncio
async def test_effective_position_prefers_broker_qty_with_db_metadata():
    broker = _StubBroker([])
    broker.qty_map = {"TQQQ": 88.0}
    service = OrderTruthService(
        pg=_StubPG([_position_row(age_sec=600.0, qty=126.0)]),
        broker_open_order_source=broker,
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_position(strategy_id="qqq_intraday_001", symbol="TQQQ")

    assert snapshot.source == "broker_primary"
    assert snapshot.position.pos_qty == 88.0
    assert snapshot.position.avg_px == 48.12


@pytest.mark.asyncio
async def test_effective_position_falls_back_to_db_when_broker_unavailable():
    broker = _StubBroker(error=RuntimeError("ib down"))
    service = OrderTruthService(
        pg=_StubPG([_position_row(age_sec=600.0, qty=126.0)]),
        broker_open_order_source=broker,
        pending_grace_sec=30.0,
    )

    snapshot = await service.effective_position(strategy_id="qqq_intraday_001", symbol="TQQQ")

    assert snapshot.source == "db_fallback"
    assert snapshot.position.pos_qty == 126.0


@pytest.mark.asyncio
async def test_effective_position_uses_runtime_reconciled_flat_truth():
    broker = _StubBroker(error=RuntimeError("ib down"))
    service = OrderTruthService(
        pg=_StubPG([_position_row(age_sec=5.0, qty=126.0)]),
        broker_open_order_source=broker,
        pending_grace_sec=30.0,
        runtime_snapshot_ttl_sec=90.0,
    )
    service.publish_reconciled_open_orders(
        db_orders=[],
        ib_orders=[],
        published_at=datetime.now(timezone.utc),
    )
    service.publish_reconciled_positions(
        ib_positions={},
        ledger_positions={},
        mismatches=[],
        published_at=datetime.now(timezone.utc),
    )

    snapshot = await service.effective_position(strategy_id="qqq_intraday_001", symbol="TQQQ")

    assert snapshot.source == "runtime_reconciled_flat"
    assert snapshot.position.pos_qty == 0.0


@pytest.mark.asyncio
async def test_effective_position_does_not_flatten_when_strategy_has_open_order():
    broker = _StubBroker(error=RuntimeError("ib down"))
    row = _db_row(age_sec=5.0, order_id="oid-pos-open", order_ref="ref-pos-open")
    service = OrderTruthService(
        pg=_StubPG([_position_row(age_sec=5.0, qty=126.0), row]),
        broker_open_order_source=broker,
        pending_grace_sec=30.0,
        runtime_snapshot_ttl_sec=90.0,
    )
    service.publish_reconciled_open_orders(
        db_orders=[row],
        ib_orders=[],
        published_at=datetime.now(timezone.utc),
    )
    service.publish_reconciled_positions(
        ib_positions={},
        ledger_positions={},
        mismatches=[],
        published_at=datetime.now(timezone.utc),
    )

    snapshot = await service.effective_position(strategy_id="qqq_intraday_001", symbol="TQQQ")

    assert snapshot.source != "runtime_reconciled_flat"
    assert snapshot.position.pos_qty == 126.0
