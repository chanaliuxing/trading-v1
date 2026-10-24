from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.exec.models import quarantine_matches_intent


def _intent(**overrides):
    base = {
        "account_id": "A1",
        "strategy_id": "STRAT_A",
        "strategy_instance_id": "STRAT_A_1",
        "symbol": "SPY",
        "intent_id": "intent-a",
        "client_order_id": "client-a",
    }
    base.update(overrides)
    return base


def _quarantine(**overrides):
    base = {
        "quarantine_scope": "STRATEGY_INSTANCE",
        "account_id": "A1",
        "strategy_id": "STRAT_A",
        "strategy_instance_id": "STRAT_A_1",
        "symbol": "SPY",
        "status": "active",
    }
    base.update(overrides)
    return base


def _service(rows, *, enable_execution=True):
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=list(rows))
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value=None)
    return ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=enable_execution,
        order_event_enabled=False,
        flip_guard_enabled=False,
    )


def _result(*, extras: dict) -> PipelineResult:
    return PipelineResult(
        status="APPROVED",
        decision_id="d-q",
        strategy_id="STRAT_A",
        symbol="SPY",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=1,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=0.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": dict(extras)},
    )


@pytest.mark.asyncio
async def test_strategy_instance_quarantine_blocks_only_matching_instance():
    svc = _service([_quarantine()])

    active = await svc._has_unresolved_symbol_quarantine(**_intent())

    assert active is True


@pytest.mark.asyncio
async def test_strategy_instance_quarantine_does_not_block_other_strategy_same_symbol():
    svc = _service([_quarantine()])

    active = await svc._has_unresolved_symbol_quarantine(
        **_intent(strategy_id="STRAT_B", strategy_instance_id="STRAT_B_1", intent_id="intent-b")
    )

    assert active is False


@pytest.mark.asyncio
async def test_account_symbol_quarantine_blocks_all_strategies_for_symbol():
    svc = _service([_quarantine(quarantine_scope="ACCOUNT_SYMBOL", strategy_instance_id="STRAT_A_1")])

    active = await svc._has_unresolved_symbol_quarantine(
        **_intent(strategy_id="STRAT_B", strategy_instance_id="STRAT_B_1", intent_id="intent-b")
    )

    assert active is True


@pytest.mark.asyncio
async def test_global_quarantine_blocks_all_entry_intents():
    svc = _service([_quarantine(quarantine_scope="GLOBAL", account_id=None, symbol=None)])

    active = await svc._has_unresolved_symbol_quarantine(
        **_intent(account_id="A2", strategy_id="STRAT_B", strategy_instance_id="STRAT_B_1", symbol="QQQ")
    )

    assert active is True


@pytest.mark.asyncio
async def test_legacy_symbol_quarantine_is_conservative_account_symbol_scope():
    svc = _service([
        _quarantine(
            quarantine_scope=None,
            account_id="A1",
            strategy_id=None,
            strategy_instance_id=None,
            symbol="SPY",
        )
    ])

    same_account = await svc._has_unresolved_symbol_quarantine(
        **_intent(strategy_id="STRAT_B", strategy_instance_id="STRAT_B_1")
    )
    other_account = await svc._has_unresolved_symbol_quarantine(
        **_intent(account_id="A2", strategy_id="STRAT_B", strategy_instance_id="STRAT_B_1")
    )

    assert same_account is True
    assert other_account is False


@pytest.mark.asyncio
async def test_legacy_quarantine_schema_fallback_does_not_block_when_clear():
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        RuntimeError('column "quarantine_scope" does not exist'),
        [],
    ])
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
    )

    active = await svc._has_unresolved_symbol_quarantine(symbol="SPY")

    assert active is False


@pytest.mark.asyncio
async def test_legacy_quarantine_schema_fallback_blocks_matching_symbol():
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        RuntimeError('column "quarantine_scope" does not exist'),
        [{"quarantine_scope": None, "symbol": "SPY", "status": "active"}],
    ])
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
    )

    active = await svc._has_unresolved_symbol_quarantine(symbol="SPY")

    assert active is True


@pytest.mark.asyncio
async def test_close_only_cleanup_not_blocked_by_entry_quarantine_gate(monkeypatch):
    svc = _service([_quarantine(quarantine_scope="GLOBAL")])
    svc._has_unresolved_symbol_quarantine = AsyncMock(return_value=True)
    svc.risk.approve = AsyncMock(return_value=SimpleNamespace(approved=False, reason="risk_block", hits=[]))
    svc._emit_event = AsyncMock(return_value=None)

    out = await svc.execute(_result(extras={"close_only": True, "reduce_only": True}))

    svc._has_unresolved_symbol_quarantine.assert_not_awaited()
    assert out.stage == "risk"


def test_quarantine_matches_intent_order_owner_exact_keys():
    assert quarantine_matches_intent(
        _quarantine(quarantine_scope="ORDER_OWNER", intent_id="intent-a"),
        _intent(),
        live_mode=True,
    )
    assert not quarantine_matches_intent(
        _quarantine(quarantine_scope="ORDER_OWNER", intent_id="intent-a"),
        _intent(intent_id="intent-b"),
        live_mode=True,
    )
