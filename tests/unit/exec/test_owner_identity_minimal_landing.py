from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.exec.models import IntentFlags, OrderIntent, OrderRole, OwnerKey, QuarantineScope


class IBAdapter:
    pass


def _plan(*, side: str = "BUY") -> ExecPlan:
    return ExecPlan(
        order_type="MKT",
        side=side,
        qty=1.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=0.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )


def _result(*, side: str = "BUY", extras: dict | None = None) -> PipelineResult:
    return PipelineResult(
        status="APPROVED",
        decision_id="d-owner-1",
        strategy_id="strat_a",
        symbol="SPY",
        plan=_plan(side=side),
        metadata={"envelope_extras": dict(extras or {})},
    )


def _service() -> ExecutionService:
    pg = SimpleNamespace(
        fetchone=AsyncMock(return_value=None),
        execute=AsyncMock(return_value="OK"),
    )
    svc = ExecutionService(
        pg=pg,
        risk_service=SimpleNamespace(approve=AsyncMock(), release=AsyncMock()),
        safety_kernel=SimpleNamespace(broker=IBAdapter()),
        enable_execution=True,
        order_event_enabled=False,
        flatten_controller=SimpleNamespace(),
        flip_guard_enabled=False,
    )
    svc._schedule_event_emit = lambda _event: None
    return svc


def test_owner_identity_models_are_first_class():
    owner = OwnerKey(
        account_id="DU123",
        strategy_id="strat_a",
        strategy_instance_id="strat_a_001",
        symbol="SPY",
        intent_id="intent-1",
    )
    flags = IntentFlags(reduce_only=True, close_only=True, same_symbol_multi_strategy_safe=False)
    intent = OrderIntent(
        decision_id="d1",
        strategy_id=owner.strategy_id,
        account_id=owner.account_id,
        strategy_instance_id=owner.strategy_instance_id,
        intent_id=owner.intent_id,
        client_order_id="client-1",
        symbol=owner.symbol,
        side="BUY",
        qty=1,
        order_type="MKT",
        order_role=OrderRole.ENTRY,
        reduce_only=flags.reduce_only,
        same_symbol_multi_strategy_safe=flags.same_symbol_multi_strategy_safe,
        ib_connection_epoch=9,
        source_event_id="evt-1",
    )

    assert intent.account_id == "DU123"
    assert intent.strategy_instance_id == "strat_a_001"
    assert intent.intent_id == "intent-1"
    assert intent.client_order_id == "client-1"
    assert intent.order_role == OrderRole.ENTRY
    assert intent.reduce_only is True
    assert intent.close_only is False
    assert intent.same_symbol_multi_strategy_safe is False
    assert intent.ib_connection_epoch == 9
    assert intent.source_event_id == "evt-1"
    assert intent.owner_key().strategy_instance_id == "strat_a_001"
    assert intent.requires_owner_identity() is True
    assert intent.is_entry() is False
    assert intent.is_close_or_reduce() is True
    assert intent.validate_owner_identity(strict_live=True) == (True, "ok")
    assert QuarantineScope.STRATEGY_INSTANCE.value == "STRATEGY_INSTANCE"


def test_live_entry_rejects_missing_owner_fields_on_intent_model():
    intent = OrderIntent(
        decision_id="d1",
        strategy_id="strat_a",
        symbol="SPY",
        side="BUY",
        qty=1,
        order_type="MKT",
        order_role=OrderRole.ENTRY,
        intent_id="intent-1",
    )

    ok, reason = intent.validate_owner_identity(strict_live=True)

    assert ok is False
    assert "account_id" in reason
    assert "strategy_instance_id" in reason


def test_paper_or_test_legacy_intent_can_use_explicit_legacy_owner_fallback():
    intent = OrderIntent(
        decision_id="d1",
        strategy_id="legacy_strat",
        account_id="DU123",
        intent_id="intent-1",
        symbol="SPY",
        side="BUY",
        qty=1,
        order_type="MKT",
        order_role=OrderRole.ENTRY,
    )

    assert intent.validate_owner_identity(strict_live=False) == (True, "ok")
    assert intent.owner_key(legacy_test_fallback=True).strategy_instance_id == "legacy_strat"


def test_reduce_only_cannot_allow_reverse():
    intent = OrderIntent(
        decision_id="d1",
        strategy_id="strat_a",
        account_id="DU123",
        strategy_instance_id="strat_a_001",
        intent_id="intent-1",
        symbol="SPY",
        side="SELL",
        qty=1,
        order_type="MKT",
        order_role=OrderRole.EXIT,
        reduce_only=True,
        allow_reverse=True,
    )

    assert intent.validate_owner_identity(strict_live=True) == (
        False,
        "close_or_reduce_only_cannot_allow_reverse",
    )


@pytest.mark.asyncio
async def test_live_entry_rejects_missing_owner_fields_before_risk_or_broker():
    svc = _service()

    out = await svc.execute(_result(extras={"intent_id": "intent-missing-owner"}))

    assert out.success is False
    assert out.reason == "live_entry_owner_identity_missing"
    assert out.stage == "validation"
    svc.risk.approve.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_entry_rejects_close_only_entry_semantics():
    svc = _service()

    out = await svc.execute(
        _result(
            extras={
                "account_id": "DU123",
                "strategy_instance_id": "strat_a_001",
                "intent_id": "intent-close-only-entry",
                "order_role": "ENTRY",
                "close_only": True,
            }
        )
    )

    assert out.success is False
    assert out.reason == "entry_cannot_be_close_or_reduce_only"
    svc.risk.approve.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_sell_short_entry_requires_explicit_opposite_direction_policy():
    svc = _service()

    out = await svc.execute(
        _result(
            side="SELL_SHORT",
            extras={
                "account_id": "DU123",
                "strategy_instance_id": "strat_a_001",
                "intent_id": "intent-short-entry",
                "order_role": "ENTRY",
            },
        )
    )

    assert out.success is False
    assert out.reason == "opposite_direction_same_symbol_requires_explicit_policy"
    svc.risk.approve.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_same_symbol_safe_flag_without_arbiter_is_not_enough():
    svc = _service()

    out = await svc.execute(
        _result(
            side="SELL_SHORT",
            extras={
                "account_id": "DU123",
                "strategy_instance_id": "strat_a_001",
                "intent_id": "intent-short-entry",
                "order_role": "ENTRY",
                "same_symbol_multi_strategy_safe": True,
            },
        )
    )

    assert out.success is False
    assert out.reason == "same_symbol_multi_strategy_safe_requires_execution_arbiter"
    svc.risk.approve.assert_not_awaited()


def test_close_only_exit_not_blocked_by_entry_owner_gate():
    svc = _service()

    ok, reason, _meta = svc._validate_live_entry_owner_contract(
        strategy_id="strat_a",
        symbol="SPY",
        plan=_plan(side="SELL"),
        idempotency_key="intent-exit-1",
        envelope_extras={"order_role": "EXIT", "close_only": True, "reduce_only": True},
        is_non_entry_flow=True,
    )

    assert ok is True
    assert reason == "ok"


def test_stale_event_guard_adds_source_event_and_epoch_to_payload(monkeypatch):
    svc = _service()
    monkeypatch.setattr(
        ExecutionService,
        "_current_ib_order_identity",
        staticmethod(lambda: {"ib_client_id": 11, "ib_session_role": "exec", "ib_connection_epoch": 42}),
    )
    event = {}

    def _capture(event_arg):
        event.update(event_arg)

    monkeypatch.setattr(svc, "_schedule_event_emit", _capture)

    import asyncio

    asyncio.run(
        svc._emit_event(
            event_type="intent_received",
            trace_ctx=SimpleNamespace(
                trace_id="t1",
                causation_id=None,
                strategy_id="strat_a",
                strategy_run_id="run_1",
                trigger_id="trg_1",
                decision_id="d1",
            ),
            symbol="SPY",
            intent_id="intent-1",
            payload={},
        )
    )

    assert event["source_event_id"] == "intent_received:intent-1:42"
    assert event["ib_connection_epoch"] == 42
    assert event["payload"]["source_event_id"] == "intent_received:intent-1:42"
    assert event["payload"]["ib_connection_epoch"] == 42
