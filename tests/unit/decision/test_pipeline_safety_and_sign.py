import pytest
from unittest.mock import AsyncMock

from libs.strategy.base import DecisionEnvelope, Freshness, OrderStyle
from services.decision.pipeline import DecisionPipelineService
from services.exec.execution_service import ExecutionResult
from services.data_availability import get_data_availability_service


class _MS:
    async def get(self, symbol):
        # Enough for translator to compute a plan without triggering ClickHouse fallback.
        return {
            "halted": False,
            "bid_px": 99.9,
            "ask_px": 100.1,
            "mid_px": 100.0,
            "last_px": 100.0,
            "tick_size": 0.01,
        }


class _Bars:
    async def window(self, symbol, end_us, count):
        return []


class _Ledger:
    async def snapshot(self):
        return {"items": {}}


@pytest.mark.asyncio
async def test_pipeline_preserves_sell_sign_after_arbiter():
    ms = _MS()
    bars = _Bars()
    feats = AsyncMock()
    ledger = _Ledger()

    pipe = DecisionPipelineService(
        market_state_provider=ms,
        bar_store=bars,
        feature_store=feats,
        reserve_ledger=ledger,
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=True,
        bypass_validator=True,  # focus on arbiter->translator sign path
        bypass_arbiter=False,
    )

    env = DecisionEnvelope(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"ref_px": 100.0, "edge_bp": 10.0},
    )

    res = await pipe.process_decision(env)
    assert res.status == "APPROVED"
    assert res.plan is not None
    assert res.plan.side == "SELL"
    assert res.plan.qty == 10.0


@pytest.mark.asyncio
async def test_pipeline_finalizes_arbiter_allocation_when_no_execution_service():
    ms = _MS()
    bars = _Bars()
    feats = AsyncMock()
    ledger = _Ledger()

    pipe = DecisionPipelineService(
        market_state_provider=ms,
        bar_store=bars,
        feature_store=feats,
        reserve_ledger=ledger,
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
    )
    pipe.arbiter.finalize = AsyncMock(return_value=None)

    env = DecisionEnvelope(
        decision_id="d-finalize",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"ref_px": 100.0, "edge_bp": 10.0},
    )

    res = await pipe.process_decision(env)
    assert res.status == "APPROVED"
    pipe.arbiter.finalize.assert_awaited_once_with("d-finalize", "SHADOW_APPROVED")


@pytest.mark.asyncio
async def test_pipeline_strategy_tradability_gate_is_scoped_to_strategy():
    service = get_data_availability_service()
    service.set_durable_state_path(None)
    service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="QQQ",
        resolution="entry",
        reason="data_integrity_fail_closed",
        strategy_id="blocked_strategy",
        stage="stable_entry_guard",
    )
    try:
        pipe = DecisionPipelineService(
            market_state_provider=_MS(),
            bar_store=_Bars(),
            feature_store=AsyncMock(),
            reserve_ledger=_Ledger(),
            adv_provider=None,
            tick_size_provider=None,
            shadow_mode=True,
            bypass_validator=True,
            bypass_arbiter=False,
        )

        env = DecisionEnvelope(
            decision_id="d-scoped-data",
            strategy_id="other_strategy",
            symbol="SOXS",
            ts_bar_end_us=1,
            target_qty=-10.0,
            freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
            max_notional=100000.0,
            extras={"ref_px": 100.0, "edge_bp": 10.0},
        )

        res = await pipe.process_decision(env)

        assert res.status == "APPROVED"
    finally:
        service.clear(
            dataset="strategy_tradability",
            symbol="QQQ",
            resolution="entry",
            strategy_id="blocked_strategy",
            stage="stable_entry_guard",
        )


@pytest.mark.asyncio
async def test_pipeline_strategy_tradability_gate_blocks_matching_strategy():
    service = get_data_availability_service()
    service.set_durable_state_path(None)
    service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="QQQ",
        resolution="entry",
        reason="data_integrity_fail_closed",
        strategy_id="blocked_strategy",
        stage="stable_entry_guard",
    )
    try:
        pipe = DecisionPipelineService(
            market_state_provider=_MS(),
            bar_store=_Bars(),
            feature_store=AsyncMock(),
            reserve_ledger=_Ledger(),
            adv_provider=None,
            tick_size_provider=None,
            shadow_mode=True,
            bypass_validator=True,
            bypass_arbiter=False,
        )

        env = DecisionEnvelope(
            decision_id="d-matching-data",
            strategy_id="blocked_strategy",
            symbol="SQQQ",
            ts_bar_end_us=1,
            target_qty=10.0,
            freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
            max_notional=100000.0,
            extras={"ref_px": 100.0, "edge_bp": 10.0},
        )

        res = await pipe.process_decision(env)

        assert res.status == "REJECTED"
        assert res.stage == "data_availability"
    finally:
        service.clear(
            dataset="strategy_tradability",
            symbol="QQQ",
            resolution="entry",
            strategy_id="blocked_strategy",
            stage="stable_entry_guard",
        )


@pytest.mark.asyncio
async def test_pipeline_bypasses_arbiter_for_safety_intent():
    ms = _MS()
    bars = _Bars()
    feats = AsyncMock()
    ledger = _Ledger()

    pipe = DecisionPipelineService(
        market_state_provider=ms,
        bar_store=bars,
        feature_store=feats,
        reserve_ledger=ledger,
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    pipe.arbiter.admit = AsyncMock(return_value=(True, 999.0, {"reason": "should_not_be_called"}))

    env = DecisionEnvelope(
        decision_id="d2",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"flatten_action": True, "close_only": True, "ref_px": 100.0},
    )

    res = await pipe.process_decision(env)
    assert res.status == "APPROVED"
    assert res.plan is not None
    assert res.plan.side == "SELL"
    assert res.plan.qty == 10.0
    pipe.arbiter.admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_option_close_only_respects_market_order_style_without_l1():
    class _ZeroMS:
        async def get(self, symbol):
            return {
                "halted": False,
                "bid_px": 0.0,
                "ask_px": 0.0,
                "mid_px": 0.0,
                "last_px": 0.0,
                "tick_size": 0.01,
            }

    ms = _ZeroMS()
    bars = _Bars()
    feats = AsyncMock()
    ledger = _Ledger()

    pipe = DecisionPipelineService(
        market_state_provider=ms,
        bar_store=bars,
        feature_store=feats,
        reserve_ledger=ledger,
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    env = DecisionEnvelope(
        decision_id="d-close-opt",
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        ts_bar_end_us=1,
        target_qty=-4.0,
        order_style=OrderStyle(kind="MKT"),
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={
            "close_only": True,
            "intent_action": "exit",
            "instrument_type": "OPTION",
            "position_qty_hint": 4.0,
        },
    )

    res = await pipe.process_decision(env)
    assert res.status == "APPROVED"
    assert res.plan is not None
    assert res.plan.order_type == "MKT"
    assert res.plan.side == "SELL"
    assert res.plan.qty == 4.0


@pytest.mark.asyncio
async def test_pipeline_normalizes_near_integral_stock_qty_before_translation():
    ms = _MS()
    bars = _Bars()
    feats = AsyncMock()
    ledger = _Ledger()

    pipe = DecisionPipelineService(
        market_state_provider=ms,
        bar_store=bars,
        feature_store=feats,
        reserve_ledger=ledger,
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    pipe.arbiter.admit = AsyncMock(return_value=(True, 252.99999999999997, {"reason": "coexist"}))

    env = DecisionEnvelope(
        decision_id="d-float-qty",
        strategy_id="s1",
        symbol="SOXS",
        ts_bar_end_us=1,
        target_qty=-253.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=9968.2,
        extras={"ref_px": 39.400000000000006, "edge_bp": 10.0, "instrument_type": "STK"},
    )

    res = await pipe.process_decision(env)

    assert res.status == "APPROVED"
    assert res.plan is not None
    assert res.plan.side == "SELL"
    assert res.plan.qty == pytest.approx(253.0)


@pytest.mark.asyncio
async def test_pipeline_routes_flatten_to_uom_delegate():
    ms = _MS()
    bars = _Bars()
    feats = AsyncMock()
    ledger = _Ledger()

    pipe = DecisionPipelineService(
        market_state_provider=ms,
        bar_store=bars,
        feature_store=feats,
        reserve_ledger=ledger,
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=False,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    class _Delegate:
        def __init__(self):
            self.submit_flatten = AsyncMock(
                return_value=ExecutionResult(success=True, decision_id="d-flat", reason="ok")
            )
            self.execute = AsyncMock()

    delegate = _Delegate()
    pipe.set_execution_service(delegate)

    env = DecisionEnvelope(
        decision_id="d-flat",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"flatten_action": True, "close_only": True, "ref_px": 100.0},
    )

    res = await pipe.process_decision(env)

    assert res.status == "APPROVED"
    delegate.submit_flatten.assert_awaited_once()
    delegate.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_enriches_live_entry_owner_identity_from_execution_context():
    pipe = DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=AsyncMock(),
        reserve_ledger=_Ledger(),
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=False,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    class _Broker:
        account = "DU_TEST"

    class _Kernel:
        broker_adapter = _Broker()

    class _ExecutionService:
        kernel = _Kernel()

    class _Delegate:
        execution_service = _ExecutionService()

        def __init__(self):
            self.submit_entry = AsyncMock(
                return_value=ExecutionResult(success=True, decision_id="d-owner", reason="ok")
            )

    delegate = _Delegate()
    pipe.set_execution_service(delegate)

    env = DecisionEnvelope(
        decision_id="d-owner",
        strategy_id="stocks_kernel_smh_001",
        symbol="SOXL",
        ts_bar_end_us=1,
        target_qty=10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"ref_px": 100.0, "edge_bp": 10.0, "strategy_run_id": "run-1"},
    )

    res = await pipe.process_decision(env)

    assert res.status == "APPROVED"
    delegate.submit_entry.assert_awaited_once()
    submitted = delegate.submit_entry.await_args.args[0]
    extras = submitted.metadata["envelope_extras"]
    assert extras["account_id"] == "DU_TEST"
    assert extras["strategy_instance_id"] == "run-1"


@pytest.mark.asyncio
async def test_pipeline_enriches_owner_identity_from_kernel_resolved_account_id():
    pipe = DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=AsyncMock(),
        reserve_ledger=_Ledger(),
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=False,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    class _Kernel:
        @staticmethod
        def _resolved_account_id() -> str:
            return "DU_RESOLVED"

    class _ExecutionService:
        kernel = _Kernel()

    class _Delegate:
        execution_service = _ExecutionService()

        def __init__(self):
            self.submit_entry = AsyncMock(
                return_value=ExecutionResult(success=True, decision_id="d-owner-kernel", reason="ok")
            )

    delegate = _Delegate()
    pipe.set_execution_service(delegate)

    env = DecisionEnvelope(
        decision_id="d-owner-kernel",
        strategy_id="stocks_kernel_smh_001",
        symbol="SOXL",
        ts_bar_end_us=1,
        target_qty=10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"ref_px": 100.0, "edge_bp": 10.0, "strategy_run_id": "run-2"},
    )

    res = await pipe.process_decision(env)

    assert res.status == "APPROVED"
    delegate.submit_entry.assert_awaited_once()
    submitted = delegate.submit_entry.await_args.args[0]
    extras = submitted.metadata["envelope_extras"]
    assert extras["account_id"] == "DU_RESOLVED"
    assert extras["strategy_instance_id"] == "run-2"


@pytest.mark.asyncio
async def test_pipeline_uses_strategy_id_as_stable_legacy_instance_not_generated_trace_id():
    pipe = DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=AsyncMock(),
        reserve_ledger=_Ledger(),
        adv_provider=None,
        tick_size_provider=None,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
    )

    env = DecisionEnvelope(
        decision_id="d-legacy-owner",
        strategy_id="legacy_strategy_001",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=10.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras={"ref_px": 100.0, "edge_bp": 10.0},
    )

    res = await pipe.process_decision(env)

    assert res.status == "APPROVED"
    assert res.metadata["envelope_extras"]["strategy_instance_id"] == "legacy_strategy_001"
