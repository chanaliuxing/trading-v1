import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.exec.execution_service import ExecutionService
from services.exec.models import cancel_request_matches_order_owner


def _service(rows, open_orders):
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=rows)
    broker = MagicMock()
    broker.open_orders = AsyncMock(return_value=open_orders)
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker
    return ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True), broker


def _note(*, account_id="DU123", strategy_instance_id="strat_a_1", intent_id="intent-a"):
    return json.dumps(
        {
            "extras": {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "intent_id": intent_id,
            }
        }
    )


def _row(order_id, *, strategy_instance_id, order_ref=""):
    return {
        "broker_order_id": str(order_id),
        "order_type": "STP",
        "state": "SUBMITTED",
        "order_ref": order_ref,
        "strategy_id": "mean_revert",
        "symbol": "SPY",
        "note": _note(strategy_instance_id=strategy_instance_id),
    }


def _open(order_id, *, order_ref=""):
    return SimpleNamespace(order_id=order_id, symbol="SPY", order_ref=order_ref, order_type="STP")


def test_cancel_request_matches_order_owner_requires_exact_strategy_instance():
    request = {
        "account_id": "DU123",
        "strategy_id": "mean_revert",
        "strategy_instance_id": "strat_a_1",
        "symbol": "SPY",
    }

    assert cancel_request_matches_order_owner(
        request,
        {
            "account_id": "DU123",
            "strategy_id": "mean_revert",
            "strategy_instance_id": "strat_a_1",
            "symbol": "SPY",
        },
    )
    assert not cancel_request_matches_order_owner(
        request,
        {
            "account_id": "DU123",
            "strategy_id": "mean_revert",
            "strategy_instance_id": "strat_b_1",
            "symbol": "SPY",
        },
    )


def test_cancel_request_matches_order_owner_rejects_missing_owner_fields():
    assert not cancel_request_matches_order_owner(
        {"strategy_id": "mean_revert", "symbol": "SPY", "strategy_instance_id": "strat_a_1"},
        {"strategy_id": "mean_revert", "symbol": "SPY"},
    )


@pytest.mark.asyncio
async def test_strategy_a_cancel_only_cancels_durable_a_owned_order():
    svc, broker = _service(
        rows=[
            _row(101, strategy_instance_id="strat_b_1", order_ref="same-prefix-overlap"),
            _row(102, strategy_instance_id="strat_a_1", order_ref="same-prefix-owner"),
        ],
        open_orders=[
            _open(101, order_ref="same-prefix-overlap"),
            _open(102, order_ref="same-prefix-owner"),
        ],
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="mean_revert",
        symbol="SPY",
        reason="unit-test",
        account_id="DU123",
        strategy_instance_id="strat_a_1",
    )

    broker.cancel.assert_awaited_once_with(102)


@pytest.mark.asyncio
async def test_cancel_does_not_use_order_ref_prefix_or_substring_as_owner_proof():
    svc, broker = _service(
        rows=[
            {
                "broker_order_id": "201",
                "order_type": "STP",
                "state": "SUBMITTED",
                "order_ref": "mean_revert:SPY:strat_a_1-looking",
                "strategy_id": "mean_revert",
                "symbol": "SPY",
                "note": _note(strategy_instance_id="strat_b_1"),
            }
        ],
        open_orders=[_open(201, order_ref="mean_revert:SPY:strat_a_1-looking")],
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="mean_revert",
        symbol="SPY",
        reason="unit-test",
        account_id="DU123",
        strategy_instance_id="strat_a_1",
    )

    broker.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_only_unknown_order_is_not_cancelled_without_owner_row():
    svc, broker = _service(
        rows=[],
        open_orders=[_open(301, order_ref="mean_revert:SPY:strat_a_1")],
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="mean_revert",
        symbol="SPY",
        reason="unit-test",
        account_id="DU123",
        strategy_instance_id="strat_a_1",
    )

    broker.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_order_ref_only_verifies_broker_row_not_owner():
    svc, broker = _service(
        rows=[
            _row(401, strategy_instance_id="strat_a_1", order_ref="exact-owner-ref"),
        ],
        open_orders=[_open(401, order_ref="different-ref")],
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="mean_revert",
        symbol="SPY",
        reason="unit-test",
        account_id="DU123",
        strategy_instance_id="strat_a_1",
    )

    broker.cancel.assert_not_awaited()
