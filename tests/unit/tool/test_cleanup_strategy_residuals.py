from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import argparse

import pytest
import sys
import types
from strategies.stocks_kernel.runtime.audit import query_audit_chain


class _DummyStock:
    def __init__(self, symbol, exchange, currency):
        self.secType = "STK"
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


class _DummyLimitOrder:
    def __init__(self, action, totalQuantity, lmtPrice):
        self.action = action
        self.totalQuantity = totalQuantity
        self.lmtPrice = lmtPrice
        self.orderType = "LMT"
        self.outsideRth = False


class _DummyMarketOrder:
    def __init__(self, action, totalQuantity):
        self.action = action
        self.totalQuantity = totalQuantity
        self.orderType = "MKT"
        self.outsideRth = False


class _DummyOption:
    def __init__(self, symbol, lastTradeDateOrContractMonth, strike, right, exchange="SMART", currency="USD", multiplier="100"):
        self.secType = "OPT"
        self.symbol = symbol
        self.lastTradeDateOrContractMonth = lastTradeDateOrContractMonth
        self.strike = strike
        self.right = right
        self.exchange = exchange
        self.currency = currency
        self.multiplier = multiplier


sys.modules.setdefault(
    "ib_insync",
    types.SimpleNamespace(
        IB=object,
        LimitOrder=_DummyLimitOrder,
        MarketOrder=_DummyMarketOrder,
        Stock=_DummyStock,
        Option=_DummyOption,
    ),
)

from tool.cleanup_strategy_residuals import (
    _adhoc_ib_connect_allowed,
    BrokerResidualPosition,
    DbResidualOrder,
    DbResidualPosition,
    build_broker_cleanup_orders,
    build_db_order_cleanup_plan,
    build_db_position_cleanup_plan,
    build_explicit_position_cleanup_plan,
    cancel_broker_orders,
    clear_manual_locks_for_targets,
    converge_position_ledgers_flat,
    fetch_broker_positions,
    flatten_broker_residuals,
    release_db_order_reservations,
    summarize_cleanup_state,
    terminalize_protection_registry_for_targets,
    terminalize_protection_registry_for_orders,
    verify_broker_absent_for_db_orders,
    wait_for_broker_clean,
    wait_for_broker_open_orders_absent,
    write_cleanup_audit,
)
from libs.options.internal_symbol import format_internal_opt_symbol


def test_build_broker_cleanup_orders_for_overnight():
    orders = build_broker_cleanup_orders(
        broker_positions=[
            BrokerResidualPosition(symbol="QQQ", qty=-1),
            BrokerResidualPosition(symbol="SQQQ", qty=1),
        ],
        session="overnight",
    )

    assert [item.symbol for item in orders] == ["QQQ", "SQQQ"]
    assert orders[0].action == "BUY"
    assert orders[0].exchange == "OVERNIGHT"
    assert orders[0].tif == "DAY"
    assert orders[0].outside_rth is True
    assert orders[1].action == "SELL"


def test_adhoc_connect_allowed_default_and_disable_flag(monkeypatch):
    monkeypatch.delenv("IBKR_DISABLE_ADHOC_CONNECT", raising=False)
    assert _adhoc_ib_connect_allowed(argparse.Namespace()) is True
    monkeypatch.setenv("IBKR_DISABLE_ADHOC_CONNECT", "1")
    assert _adhoc_ib_connect_allowed(argparse.Namespace()) is False


def test_build_db_cleanup_plans_filter_non_terminal_and_targeted_strategy():
    position_plan = build_db_position_cleanup_plan(
        db_positions=[
            DbResidualPosition(strategy_id="qqq_intraday_001", symbol="TQQQ", pos_qty=9.0),
            DbResidualPosition(strategy_id="other_strategy", symbol="TQQQ", pos_qty=2.0),
        ],
        symbols=[],
        strategy_ids=["qqq_intraday_001"],
    )
    order_plan = build_db_order_cleanup_plan(
        db_orders=[
            DbResidualOrder(order_id="1", strategy_id="stocks_kernel_shadow_qqq_001", symbol="TQQQ", side="SELL", qty=1, state="NEW"),
            DbResidualOrder(order_id="2", strategy_id="stocks_kernel_shadow_qqq_001", symbol="TQQQ", side="SELL", qty=1, state="CANCELLED"),
        ],
        symbols=[],
        strategy_ids=["stocks_kernel_shadow_qqq_001"],
    )

    assert len(position_plan) == 1
    assert position_plan[0].strategy_id == "qqq_intraday_001"
    assert len(order_plan) == 1
    assert order_plan[0].order_id == "1"


def test_build_explicit_position_cleanup_plan_includes_flat_target():
    rows = build_explicit_position_cleanup_plan(
        symbols=["NVDA"],
        strategy_ids=["nvda_minute_flip_paper_001"],
        existing_rows=[],
    )

    assert rows == [DbResidualPosition(strategy_id="nvda_minute_flip_paper_001", symbol="NVDA", pos_qty=0.0)]


def test_summarize_cleanup_state_marks_dirty_until_all_sources_flat():
    payload = summarize_cleanup_state(
        broker_positions=[BrokerResidualPosition(symbol="QQQ", qty=-1)],
        broker_open_order_count=0,
        db_positions=[DbResidualPosition(strategy_id="qqq_intraday_001", symbol="TQQQ", pos_qty=9)],
        db_orders=[DbResidualOrder(order_id="1", strategy_id="stocks_kernel_shadow_qqq_001", symbol="TQQQ", side="SELL", qty=1, state="NEW")],
    )

    assert payload["clean"] is False
    assert payload["broker_positions"][0]["symbol"] == "QQQ"
    assert payload["db_positions"][0]["strategy_id"] == "qqq_intraday_001"
    assert payload["db_orders"][0]["state"] == "NEW"


def test_write_cleanup_audit_emits_recon_event(tmp_path: Path):
    write_cleanup_audit(
        audit_root=tmp_path,
        strategy_id="qqq_intraday_001",
        stage="residual_cleanup_started",
        reason="preview",
        payload={"symbols": ["QQQ", "TQQQ", "SQQQ"]},
    )

    rows = query_audit_chain(root_dir=tmp_path, strategy_id="qqq_intraday_001", domain="recon")
    assert len(rows) == 1
    assert rows[0]["stage"] == "residual_cleanup_started"
    assert rows[0]["reason"] == "preview"


@pytest.mark.asyncio
async def test_release_db_order_reservations_releases_only_rows_with_idempotency_key(monkeypatch):
    fake_service = MagicMock()
    fake_service.release_on_terminal = AsyncMock(return_value=(True, 123.45))
    monkeypatch.setattr("tool.cleanup_strategy_residuals.ReservationService", lambda db: fake_service)

    result = await release_db_order_reservations(
        pg=MagicMock(),
        rows=[
            DbResidualOrder(order_id="1", strategy_id="s1", symbol="SOXL", side="BUY", qty=1, state="SUBMITTED", idempotency_key="idem:1"),
            DbResidualOrder(order_id="2", strategy_id="s1", symbol="SOXS", side="SELL", qty=1, state="SUBMITTED", idempotency_key=None),
        ],
        terminal_state="CANCELLED",
        reason="cleanup",
    )

    assert result == {"attempted": 1, "released_count": 1, "released_usd": 123.45}
    fake_service.release_on_terminal.assert_awaited_once_with(
        idempotency_key="idem:1",
        order_id="1",
        terminal_state="CANCELLED",
        reason="cleanup",
    )


@pytest.mark.asyncio
async def test_converge_position_ledgers_flat_appends_offset_adjustment():
    class FakePg:
        def __init__(self):
            self.fetchone_calls = []

        async def fetchall(self, sql, *args):
            assert "FROM core.position_ledger_event" in sql
            return [{"account_id": "DUO112752", "qty": 1.0}]

        async def fetchone(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            return {"ledger_event_id": "event-1"}

    pg = FakePg()
    result = await converge_position_ledgers_flat(
        pg=pg,
        rows=[DbResidualPosition(strategy_id="nvda_minute_flip_paper_001", symbol="NVDA", pos_qty=0.0)],
    )

    assert result[0]["previous_net_qty"] == 1.0
    assert result[0]["adjustment_qty"] == -1.0
    assert result[0]["inserted"] is True
    insert_args = pg.fetchone_calls[0][1]
    assert insert_args[0] == "DUO112752"
    assert insert_args[1] == "nvda_minute_flip_paper_001"
    assert insert_args[2] == "NVDA"
    assert insert_args[6] == -1.0


@pytest.mark.asyncio
async def test_cleanup_clears_manual_locks_and_terminalizes_protection_registry():
    class FakePg:
        def __init__(self):
            self.calls = []

        async def fetchall(self, sql, *args):
            if "FROM core.manual_intervention_lock" in sql:
                return [{"present": 1}]
            if "FROM core.core_protection_order_registry" in sql:
                return [{"present": 1}, {"present": 1}]
            return []

        async def execute(self, sql, *args):
            self.calls.append((sql, args))

    pg = FakePg()
    rows = [DbResidualPosition(strategy_id="nvda_minute_flip_paper_001", symbol="NVDA", pos_qty=0.0)]

    locks = await clear_manual_locks_for_targets(pg=pg, rows=rows)
    registry = await terminalize_protection_registry_for_targets(pg=pg, rows=rows)

    assert locks == 1
    assert registry == 2
    assert any("DELETE FROM core.manual_intervention_lock" in call[0] for call in pg.calls)
    assert any("UPDATE core.core_protection_order_registry" in call[0] for call in pg.calls)


@pytest.mark.asyncio
async def test_cleanup_terminalizes_protection_registry_by_absent_order_identity():
    class FakePg:
        def __init__(self):
            self.calls = []

        async def fetchall(self, sql, *args):
            assert "core.core_protection_order_registry" in sql
            assert args[0] == "s1"
            assert args[1] == "NVDA"
            assert args[2] == "596"
            assert args[3] == "tp:DUO|s1|NVDA|"
            return [{"present": 1}]

        async def execute(self, sql, *args):
            self.calls.append((sql, args))

    pg = FakePg()
    count = await terminalize_protection_registry_for_orders(
        pg=pg,
        rows=[
            DbResidualOrder(
                order_id="db-1",
                strategy_id="s1",
                symbol="NVDA",
                side="SELL",
                qty=1,
                state="CANCELLED",
                broker_order_id="596",
                order_ref="tp:DUO|s1|NVDA|",
            )
        ],
    )

    assert count == 1
    assert any("residual_cleanup_broker_verified_absent_order" in call[0] for call in pg.calls)


@pytest.mark.asyncio
async def test_flatten_broker_residuals_uses_aggressive_limit_order_overnight():
    ib = MagicMock()
    ib.reqTickersAsync = AsyncMock(
        return_value=[SimpleNamespace(marketPrice=lambda: 50.0, last=50.0, close=49.5, bid=49.9, ask=50.1)]
    )
    ib.placeOrder = MagicMock(
        return_value=SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"))
    )

    orders = build_broker_cleanup_orders(
        broker_positions=[BrokerResidualPosition(symbol="SOXL", qty=208.0)],
        session="overnight",
    )

    result = await flatten_broker_residuals(ib=ib, orders=orders)

    contract, order = ib.placeOrder.call_args.args
    assert contract.symbol == "SOXL"
    assert order.orderType == "LMT"
    assert order.lmtPrice == pytest.approx(45.0)
    assert order.outsideRth is True
    assert result[0]["order_type"] == "LMT"
    assert result[0]["limit_px"] == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_wait_for_broker_clean_reports_dirty_when_position_or_order_remains():
    ib = MagicMock()
    ib.positions.return_value = [SimpleNamespace(contract=SimpleNamespace(symbol="NVDA"), position=1.0)]
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    ib.openTrades.return_value = [SimpleNamespace(contract=SimpleNamespace(symbol="NVDA"))]

    result = await wait_for_broker_clean(ib=ib, symbols=["NVDA"], timeout_sec=0.0, poll_sec=0.01)

    assert result["clean"] is False
    assert result["broker_positions"][0]["symbol"] == "NVDA"
    assert result["broker_open_order_count"] == 1


@pytest.mark.asyncio
async def test_wait_for_broker_open_orders_absent_blocks_until_cancel_confirmed():
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    ib.openTrades.return_value = [SimpleNamespace(contract=SimpleNamespace(symbol="NVDA"))]

    result = await wait_for_broker_open_orders_absent(
        ib=ib,
        symbols=["NVDA"],
        timeout_sec=0.0,
        poll_sec=0.01,
    )

    assert result["clean"] is False
    assert result["broker_open_order_count"] == 1


@pytest.mark.asyncio
async def test_verify_broker_absent_for_db_orders_blocks_live_matching_order():
    order = SimpleNamespace(orderId=380, orderRef="nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA", action="SELL", totalQuantity=1)
    status = SimpleNamespace(status="Submitted")
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    ib.openTrades.return_value = [
        SimpleNamespace(contract=SimpleNamespace(symbol="NVDA"), order=order, orderStatus=status)
    ]

    result = await verify_broker_absent_for_db_orders(
        ib=ib,
        symbols=["NVDA"],
        rows=[
            DbResidualOrder(
                order_id="db-1",
                strategy_id="nvda_minute_flip_paper_001",
                symbol="NVDA",
                side="SELL",
                qty=1,
                state="SUBMITTED",
                broker_order_id="380",
                order_ref="nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
            )
        ],
    )

    assert result["clean"] is False
    assert result["open_matches"][0]["broker_order"]["broker_order_id"] == "380"


@pytest.mark.asyncio
async def test_fetch_broker_positions_maps_internal_option_symbol():
    opt_contract = SimpleNamespace(
        secType="OPT",
        symbol="SPY",
        lastTradeDateOrContractMonth="20260402",
        strike=655.0,
        right="C",
    )
    ib = MagicMock()
    ib.positions.return_value = [SimpleNamespace(contract=opt_contract, position=-28.0)]

    rows = await fetch_broker_positions(
        ib=ib,
        symbols=[format_internal_opt_symbol(underlying="SPY", expiry_ymd="20260402", strike=655.0, right="C")],
    )

    assert len(rows) == 1
    assert rows[0].symbol == "OPT:SPY:20260402:655:C"
    assert rows[0].qty == -28.0


@pytest.mark.asyncio
async def test_flatten_broker_residuals_builds_option_contract_for_internal_symbol():
    ib = MagicMock()
    ib.reqTickersAsync = AsyncMock(
        return_value=[SimpleNamespace(marketPrice=lambda: 0.23, last=0.23, close=0.22, bid=0.22, ask=0.24)]
    )
    ib.placeOrder = MagicMock(
        return_value=SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"))
    )

    orders = build_broker_cleanup_orders(
        broker_positions=[
            BrokerResidualPosition(symbol="OPT:SPY:20260402:655:C", qty=-28.0),
        ],
        session="overnight",
    )

    result = await flatten_broker_residuals(ib=ib, orders=orders)

    contract, order = ib.placeOrder.call_args.args
    assert contract.secType == "OPT"
    assert contract.symbol == "SPY"
    assert str(contract.lastTradeDateOrContractMonth) == "20260402"
    assert float(contract.strike) == 655.0
    assert contract.right == "C"
    assert order.action == "BUY"
    assert result[0]["symbol"] == "OPT:SPY:20260402:655:C"


@pytest.mark.asyncio
async def test_cancel_broker_orders_matches_internal_option_symbol():
    opt_contract = SimpleNamespace(
        secType="OPT",
        symbol="SPY",
        lastTradeDateOrContractMonth="20260402",
        strike=655.0,
        right="C",
    )
    trade = SimpleNamespace(contract=opt_contract, order=SimpleNamespace(orderId=207))
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    ib.openTrades.return_value = [trade]

    result = await cancel_broker_orders(
        ib=ib,
        symbols=["OPT:SPY:20260402:655:C"],
        global_cancel=False,
    )

    ib.cancelOrder.assert_called_once_with(trade.order)
    assert result["cancelled"] == 1
