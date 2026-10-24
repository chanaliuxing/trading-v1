import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.decision.translator import ExecPlan
from services.exec.execution_service import BrokerTruthUnavailableError, ExecutionService


def test_runtime_recovery_flatten_order_ref_is_owner_scoped_when_missing():
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=False)

    extras = svc._ensure_owner_scoped_non_entry_order_ref(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        envelope_extras={
            "runtime_recovery_action": True,
            "position_safety_recovery": True,
            "flatten_action": True,
            "close_only": True,
            "account_id": "DUO112752",
            "strategy_instance_id": "nvda_minute_flip_paper_001",
            "position_lot_id": "lot-nvda-001",
        },
        idempotency_key="position_safety_recovery:2026-05-13:nvda_minute_flip_paper_001:NVDA:long",
        is_non_entry_flow=True,
    )

    assert extras["order_ref"].startswith("rflat:DUO112752|nvda_minute_flip_paper_001|NVDA|lot-nvda-001|")
    assert not extras["order_ref"].startswith("aux:NVDA:")


def test_runtime_recovery_flatten_order_ref_not_generated_without_owner_lane():
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=False)

    extras = svc._ensure_owner_scoped_non_entry_order_ref(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        envelope_extras={
            "runtime_recovery_action": True,
            "flatten_action": True,
            "close_only": True,
            "position_lot_id": "lot-nvda-001",
        },
        idempotency_key="position_safety_recovery:2026-05-13:nvda_minute_flip_paper_001:NVDA:long",
        is_non_entry_flow=True,
    )

    assert "order_ref" not in extras


@pytest.mark.asyncio
async def test_get_live_position_qty_non_manual_flatten_uses_strategy_hint_capped_by_broker():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        envelope_extras={"flatten_action": True, "close_only": True, "position_qty_hint": 76.0},
    )

    assert qty == 76.0


@pytest.mark.asyncio
async def test_get_live_position_qty_protective_stop_blocks_when_broker_truth_unavailable():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 11.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(side_effect=RuntimeError("broker down"))
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    with pytest.raises(BrokerTruthUnavailableError):
        await svc._get_live_position_qty(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            envelope_extras={"protective_stop": True, "close_only": True, "position_qty_hint": 11.0},
        )


@pytest.mark.asyncio
async def test_get_live_position_qty_non_manual_flatten_without_hint_does_not_use_account_broker_qty():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        envelope_extras={"flatten_action": True, "close_only": True},
    )

    assert qty == 0.0


@pytest.mark.asyncio
async def test_get_live_position_qty_non_manual_protective_stop_without_hint_and_missing_row_fails_closed():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value=None)
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        envelope_extras={"protective_stop": True, "close_only": True},
    )

    assert qty == 0.0


@pytest.mark.asyncio
async def test_get_live_position_qty_hint_sign_mismatch_with_broker_fails_closed():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=-50.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        envelope_extras={"flatten_action": True, "close_only": True, "position_qty_hint": 76.0},
    )

    assert qty == 0.0


@pytest.mark.asyncio
async def test_get_live_position_qty_hint_is_capped_by_broker_qty():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=50.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        envelope_extras={"flatten_action": True, "close_only": True, "position_qty_hint": 76.0},
    )

    assert qty == 50.0


@pytest.mark.asyncio
async def test_get_live_position_qty_manual_flatten_can_use_broker_position():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        envelope_extras={"manual_intervention": True, "flatten_action": True, "close_only": True},
    )

    assert qty == 137.0


@pytest.mark.asyncio
async def test_get_live_position_qty_manual_flatten_blocks_when_broker_truth_unavailable():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 11.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(side_effect=RuntimeError("broker down"))
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    with pytest.raises(BrokerTruthUnavailableError):
        await svc._get_live_position_qty(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            envelope_extras={"manual_intervention": True, "flatten_action": True, "close_only": True},
        )


@pytest.mark.asyncio
async def test_close_only_guard_blocks_cross_strategy_sell_when_strategy_position_is_flat():
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=True)
    svc._get_live_position_qty = AsyncMock(return_value=0.0)
    plan = ExecPlan(
        order_type="MKT",
        side="SELL",
        qty=10,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )

    ok, reason = await svc._enforce_close_only_guard(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=plan,
        envelope_extras={"flatten_action": True, "close_only": True},
    )

    assert ok is False
    assert reason.startswith("close_only_no_long_position")


@pytest.mark.asyncio
async def test_close_only_guard_recovery_lane_uses_broker_truth_over_db_flat():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=1.0)
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)
    plan = ExecPlan(
        order_type="MKT",
        side="SELL",
        qty=1,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )

    ok, reason = await svc._enforce_close_only_guard(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        plan=plan,
        envelope_extras={"flatten_action": True, "close_only": True, "runtime_recovery_action": True},
    )

    assert ok is True
    assert reason == "ok"
    assert plan.qty == 1


@pytest.mark.asyncio
async def test_close_only_guard_recovery_lane_requires_broker_truth():
    broker = MagicMock()
    broker.position_qty = AsyncMock(side_effect=RuntimeError("ib unavailable"))
    kernel = MagicMock()
    kernel.broker = broker
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)
    plan = ExecPlan(
        order_type="MKT",
        side="SELL",
        qty=1,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )

    ok, reason = await svc._enforce_close_only_guard(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        plan=plan,
        envelope_extras={"flatten_action": True, "close_only": True, "runtime_recovery_action": True},
    )

    assert ok is False
    assert reason == "close_only_manual_required:broker_position_truth_unavailable"


@pytest.mark.asyncio
async def test_get_live_position_qty_option_caps_hint_by_db_position_for_close_only():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 2.0})
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_0dte_opt_001",
        symbol="OPT:QQQ:20260305:500:C",
        envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 3.0},
    )

    assert qty == 2.0


@pytest.mark.asyncio
async def test_get_live_position_qty_option_hint_sign_mismatch_with_db_fails_closed():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": -2.0})
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    qty = await svc._get_live_position_qty(
        strategy_id="qqq_0dte_opt_001",
        symbol="OPT:QQQ:20260305:500:C",
        envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 3.0},
    )

    assert qty == 0.0


@pytest.mark.asyncio
async def test_get_live_position_qty_option_hint_blocks_when_db_missing():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value=None)
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=kernel, enable_execution=True)

    with pytest.raises(BrokerTruthUnavailableError):
        await svc._get_live_position_qty(
            strategy_id="qqq_0dte_opt_001",
            symbol="OPT:QQQ:20260305:500:C",
            envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 3.0},
        )


@pytest.mark.asyncio
async def test_option_same_symbol_dual_strategy_close_only_stays_strategy_scoped():
    async def _fetchone(_sql: str, strategy_id: str, symbol: str):
        assert symbol == "OPT:QQQ:20260305:500:C"
        if strategy_id == "strategy_a":
            return {"pos_qty": 3.0}
        if strategy_id == "strategy_b":
            return {"pos_qty": 5.0}
        return None

    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=_fetchone)
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=True)

    qty_a = await svc._get_live_position_qty(
        strategy_id="strategy_a",
        symbol="OPT:QQQ:20260305:500:C",
        envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 3.0},
    )
    qty_b = await svc._get_live_position_qty(
        strategy_id="strategy_b",
        symbol="OPT:QQQ:20260305:500:C",
        envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 5.0},
    )

    assert qty_a == 3.0
    assert qty_b == 5.0

    plan_a = ExecPlan(
        order_type="MKT",
        side="SELL",
        qty=4.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )
    ok_a, reason_a = await svc._enforce_close_only_guard(
        strategy_id="strategy_a",
        symbol="OPT:QQQ:20260305:500:C",
        plan=plan_a,
        envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 4.0},
    )
    assert ok_a is True
    assert reason_a == "ok"
    assert plan_a.qty == 3.0

    plan_b = ExecPlan(
        order_type="MKT",
        side="SELL",
        qty=5.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )
    ok_b, reason_b = await svc._enforce_close_only_guard(
        strategy_id="strategy_b",
        symbol="OPT:QQQ:20260305:500:C",
        plan=plan_b,
        envelope_extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 5.0},
    )
    assert ok_b is True
    assert reason_b == "ok"
    assert plan_b.qty == 5.0
