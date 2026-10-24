from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from services.exec.execution_service import ExecutionResult
from services.exec.models import FlattenScope
from services.reconciliation.position_reconciliation import (
    StrategyVirtualPosition,
    resolve_strategy_owned_flatten_quantity,
)
from services.telegram.manual_flatten_service import ManualFlattenResult, ManualFlattenService


ACCOUNT = "DUO112752"


def _ledger_row(
    strategy_id: str,
    strategy_instance_id: str,
    symbol: str,
    qty: float,
    lot_id: str,
) -> dict:
    return {
        "account_id": ACCOUNT,
        "strategy_id": strategy_id,
        "strategy_instance_id": strategy_instance_id,
        "symbol": symbol,
        "lot_id": lot_id,
        "qty": qty,
        "avg_px": 100.0,
    }


def _svc(rows: list[dict], broker_positions: list | None = None) -> tuple[ManualFlattenService, MagicMock]:
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=rows)
    pg.fetchone = AsyncMock(return_value={"qty": 0.0})
    pg.execute = AsyncMock(return_value="OK")
    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=123, stage="completed")
    )
    adapter = SimpleNamespace(
        account=ACCOUNT,
        ib=SimpleNamespace(positions=lambda: list(broker_positions or [])),
    )
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=False,
    )
    svc._now_et = lambda: datetime(2026, 5, 12, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    return svc, execution_service


@pytest.mark.asyncio
async def test_intraday_flat_closes_only_matching_strategy_instance_position():
    rows = [
        _ledger_row("strategy_a", "strategy_a_1", "SPY", 100.0, "lot-a"),
        _ledger_row("strategy_b", "strategy_b_1", "SPY", 50.0, "lot-b"),
    ]
    svc, execution_service = _svc(rows)

    result = await svc.execute_flatten_for_strategy(trigger_id="telegram:a", strategy_id="strategy_a")

    assert result.total_orders == 1
    call = execution_service.execute.call_args.args[0]
    assert call.strategy_id == "strategy_a"
    assert call.plan.side == "SELL"
    assert float(call.plan.qty) == 100.0
    extras = call.metadata["envelope_extras"]
    assert extras["strategy_instance_id"] == "strategy_a_1"
    assert extras["lot_id"] == "lot-a"


@pytest.mark.asyncio
async def test_intraday_flat_does_not_close_other_strategy_same_symbol():
    rows = [
        _ledger_row("strategy_a", "strategy_a_1", "SPY", 100.0, "lot-a"),
        _ledger_row("strategy_b", "strategy_b_1", "SPY", 50.0, "lot-b"),
    ]
    svc, execution_service = _svc(rows)

    await svc.execute_flatten_for_strategy(trigger_id="telegram:a", strategy_id="strategy_a")

    assert execution_service.execute.await_count == 1
    called_instances = [
        call.args[0].metadata["envelope_extras"]["strategy_instance_id"]
        for call in execution_service.execute.call_args_list
    ]
    assert called_instances == ["strategy_a_1"]


@pytest.mark.asyncio
async def test_intraday_flat_uses_owner_ledger_not_broker_aggregate():
    rows = [_ledger_row("strategy_a", "strategy_a_1", "SPY", 100.0, "lot-a")]
    broker_positions = [
        SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(symbol="SPY"), position=150.0, avgCost=100.0)
    ]
    svc, execution_service = _svc(rows, broker_positions=broker_positions)

    await svc.execute_flatten_for_strategy(trigger_id="telegram:a", strategy_id="strategy_a")

    call = execution_service.execute.call_args.args[0]
    assert float(call.plan.qty) == 100.0
    assert call.metadata["envelope_extras"]["flatten_scope"] == "STRATEGY_INSTANCE"


@pytest.mark.asyncio
async def test_intraday_flat_missing_strategy_instance_fails_closed():
    svc, execution_service = _svc([])
    result = ManualFlattenResult(intervention_id="i1", trigger_id="telegram:a")

    order = await svc._submit_flatten_intent(
        account_id=ACCOUNT,
        strategy_id="strategy_a",
        strategy_instance_id="",
        symbol="SPY",
        lot_id="lot-a",
        qty=100.0,
        result=result,
        source="db_position",
        scope=FlattenScope.STRATEGY_INSTANCE,
    )

    assert order.success is False
    assert order.reason == "strategy_flatten_owner_identity_required"
    assert execution_service.execute.await_count == 0


@pytest.mark.asyncio
async def test_intraday_flat_flat_owner_position_is_noop():
    svc, execution_service = _svc([])
    result = ManualFlattenResult(intervention_id="i1", trigger_id="telegram:a")

    order = await svc._submit_flatten_intent(
        account_id=ACCOUNT,
        strategy_id="strategy_a",
        strategy_instance_id="strategy_a_1",
        symbol="SPY",
        lot_id="lot-a",
        qty=0.0,
        result=result,
        source="db_position",
        scope=FlattenScope.STRATEGY_INSTANCE,
    )

    assert order.success is True
    assert order.reason == "skip_zero_qty"
    assert execution_service.execute.await_count == 0


@pytest.mark.asyncio
async def test_intraday_flat_sets_close_only_reduce_only_and_strategy_flatten_role():
    rows = [_ledger_row("strategy_a", "strategy_a_1", "SPY", -20.0, "lot-a")]
    svc, execution_service = _svc(rows)

    await svc.execute_flatten_for_strategy(trigger_id="telegram:a", strategy_id="strategy_a")

    call = execution_service.execute.call_args.args[0]
    extras = call.metadata["envelope_extras"]
    assert call.plan.side == "BUY"
    assert extras["close_only"] is True
    assert extras["reduce_only"] is True
    assert extras["order_role"] == "STRATEGY_FLATTEN"


@pytest.mark.asyncio
async def test_eod_flat_only_closes_strategy_that_requires_flat():
    rows = [
        _ledger_row("strategy_flat_required", "flat_1", "SPY", 40.0, "lot-flat"),
        _ledger_row("strategy_overnight_allowed", "overnight_1", "SPY", 70.0, "lot-overnight"),
    ]
    svc, execution_service = _svc(rows)

    await svc.execute_flatten_for_strategy(
        trigger_id="eod:strategy_flat_required",
        strategy_id="strategy_flat_required",
    )

    assert execution_service.execute.await_count == 1
    call = execution_service.execute.call_args.args[0]
    assert call.strategy_id == "strategy_flat_required"
    assert call.metadata["envelope_extras"]["strategy_instance_id"] == "flat_1"


@pytest.mark.asyncio
async def test_account_emergency_flatten_is_explicit_and_separate():
    broker_positions = [
        SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(symbol="SPY"), position=150.0, avgCost=100.0)
    ]
    svc, execution_service = _svc([], broker_positions=broker_positions)

    await svc.execute_account_emergency_flatten(trigger_id="telegram:emergency")

    call = execution_service.execute.call_args.args[0]
    extras = call.metadata["envelope_extras"]
    assert extras["flatten_scope"] == "ACCOUNT_EMERGENCY"
    assert extras["order_role"] == "ACCOUNT_EMERGENCY_FLATTEN"
    assert extras["account_emergency"] is True


@pytest.mark.asyncio
async def test_account_emergency_flatten_can_use_broker_aggregate_only_with_emergency_scope():
    broker_positions = [
        SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(symbol="SPY"), position=150.0, avgCost=100.0)
    ]
    svc, execution_service = _svc([], broker_positions=broker_positions)

    await svc.execute_account_emergency_flatten(trigger_id="telegram:emergency")

    call = execution_service.execute.call_args.args[0]
    assert float(call.plan.qty) == 150.0
    assert call.metadata["envelope_extras"]["flatten_scope"] == "ACCOUNT_EMERGENCY"


@pytest.mark.asyncio
async def test_strategy_flatten_cannot_route_to_account_emergency_flatten():
    rows = [_ledger_row("strategy_a", "strategy_a_1", "SPY", 100.0, "lot-a")]
    broker_positions = [
        SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(symbol="SPY"), position=200.0, avgCost=100.0)
    ]
    svc, execution_service = _svc(rows, broker_positions=broker_positions)

    await svc.execute_flatten_for_strategy(trigger_id="telegram:a", strategy_id="strategy_a")

    call = execution_service.execute.call_args.args[0]
    extras = call.metadata["envelope_extras"]
    assert float(call.plan.qty) == 100.0
    assert extras["flatten_scope"] == "STRATEGY_INSTANCE"
    assert extras["account_emergency"] is False


def test_owner_flatten_quantity_resolves_only_matching_owner_lane():
    positions = [
        StrategyVirtualPosition(ACCOUNT, "strategy_a", "strategy_a_1", "SPY", "lot-a", Decimal("100")),
        StrategyVirtualPosition(ACCOUNT, "strategy_b", "strategy_b_1", "SPY", "lot-b", Decimal("50")),
    ]

    qty = resolve_strategy_owned_flatten_quantity(
        account_id=ACCOUNT,
        strategy_instance_id="strategy_a_1",
        symbol="SPY",
        lot_id="lot-a",
        ledger_positions=positions,
    )

    assert qty == Decimal("100")
