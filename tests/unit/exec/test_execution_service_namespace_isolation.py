from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.exec.execution_service import ExecutionService


def _owned_note(
    *,
    account_id: str = "DU_TEST",
    strategy_instance_id: str,
    intent_id: str = "intent-1",
) -> str:
    import json

    return json.dumps(
        {
            "extras": {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "intent_id": intent_id,
            }
        }
    )


@pytest.mark.asyncio
async def test_cancel_open_orders_for_symbol_does_not_cancel_iswv2_foreign_order():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "101",
                "order_type": "LMT",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "note": _owned_note(strategy_instance_id="other_instance"),
            }
        ]
    )

    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                order_id=101,
                symbol="TQQQ",
                order_ref="iswv2:index_super_win_001:TQQQ:ENTRY:TRG:abc123",
            )
        ]
    )
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        reason="pre_flatten:test",
        account_id="DU_TEST",
        strategy_instance_id="qqq_intraday_001",
    )

    broker.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_open_orders_for_symbol_only_cancels_matching_qqqv1_order():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "101",
                "order_type": "LMT",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "note": _owned_note(strategy_instance_id="other_instance"),
            },
            {
                "broker_order_id": "102",
                "order_type": "STP",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "order_ref": "qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:mine",
                "note": _owned_note(strategy_instance_id="qqq_intraday_001"),
            },
        ]
    )

    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                order_id=101,
                symbol="TQQQ",
                order_ref="iswv2:index_super_win_001:TQQQ:ENTRY:TRG:abc123",
            ),
            SimpleNamespace(
                order_id=102,
                symbol="TQQQ",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:mine",
            ),
        ]
    )
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker
    kernel.on_cancel = AsyncMock()

    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        reason="pre_protective_stop:test",
        account_id="DU_TEST",
        strategy_instance_id="qqq_intraday_001",
    )

    broker.cancel.assert_awaited_once_with(102)
    kernel.on_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_open_orders_for_symbol_does_not_terminalize_db_on_cancel_request():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "102",
                "order_type": "STP",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "order_ref": "qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:mine",
                "note": _owned_note(strategy_instance_id="qqq_intraday_001"),
            },
        ]
    )

    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                order_id=102,
                symbol="TQQQ",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:mine",
            ),
        ]
    )
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker
    kernel.on_cancel = AsyncMock()

    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        reason="flatten_pre_cancel:test",
        only_order_types={"STP"},
        account_id="DU_TEST",
        strategy_instance_id="qqq_intraday_001",
    )

    broker.cancel.assert_awaited_once_with(102)
    kernel.on_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_open_orders_for_option_symbol_cancels_matching_option_strategy_ref_only():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "201",
                "order_type": "STP",
                "state": "SUBMITTED",
                "strategy_id": "qqq_1dte_ma10_reject_0324_001",
                "symbol": "OPT:QQQ:20260326:530:C",
                "note": _owned_note(strategy_instance_id="qqq_1dte_ma10_reject_0324_001"),
            },
            {
                "broker_order_id": "202",
                "order_type": "LMT",
                "state": "SUBMITTED",
                "strategy_id": "qqq_1dte_ma10_reject_0324_001",
                "symbol": "OPT:QQQ:20260326:530:C",
                "note": _owned_note(strategy_instance_id="other_instance"),
            },
        ]
    )

    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                order_id=201,
                symbol="OPT:QQQ:20260326:530:C",
                order_ref="opt0dte_exit:qqq_1dte_ma10_reject_0324_001:d1",
            ),
            SimpleNamespace(
                order_id=202,
                symbol="OPT:QQQ:20260326:530:C",
                order_ref="opt0dte_exit:qqq_0dte_opt_live_001:d2",
            ),
        ]
    )
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="qqq_1dte_ma10_reject_0324_001",
        symbol="OPT:QQQ:20260326:530:C",
        reason="pre_protective_stop:test",
        account_id="DU_TEST",
        strategy_instance_id="qqq_1dte_ma10_reject_0324_001",
    )

    broker.cancel.assert_awaited_once_with(201)


@pytest.mark.asyncio
async def test_cancel_open_orders_for_option_symbol_does_not_cancel_foreign_same_symbol_option_order():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "301",
                "order_type": "STP",
                "state": "SUBMITTED",
                "strategy_id": "qqq_1dte_ma10_reject_0324_001",
                "symbol": "OPT:QQQ:20260326:530:C",
                "note": _owned_note(strategy_instance_id="other_instance"),
            }
        ]
    )

    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                order_id=301,
                symbol="OPT:QQQ:20260326:530:C",
                order_ref="opt0dte_exit:qqq_0dte_opt_live_001:d2",
            )
        ]
    )
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="qqq_1dte_ma10_reject_0324_001",
        symbol="OPT:QQQ:20260326:530:C",
        reason="pre_protective_stop:test",
        account_id="DU_TEST",
        strategy_instance_id="qqq_1dte_ma10_reject_0324_001",
    )

    broker.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_open_orders_for_option_symbol_matches_exact_db_order_ref_for_optk_orders():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "401",
                "order_type": "LMT",
                "state": "SUBMITTED",
                "order_ref": "optk:optk.spy_ma_cross_stack_cont_v2_dual.spy",
                "strategy_id": "spy_1dte_ma10_reject_0324_001",
                "symbol": "OPT:SPY:20260402:655:C",
                "note": _owned_note(strategy_instance_id="spy_1dte_ma10_reject_0324_001"),
            }
        ]
    )

    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                order_id=401,
                symbol="OPT:SPY:20260402:655:C",
                order_ref="optk:optk.spy_ma_cross_stack_cont_v2_dual.spy",
            )
        ]
    )
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
    )

    await svc._cancel_open_orders_for_symbol(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        reason="pre_flatten:test",
        account_id="DU_TEST",
        strategy_instance_id="spy_1dte_ma10_reject_0324_001",
    )

    broker.cancel.assert_awaited_once_with(401)
