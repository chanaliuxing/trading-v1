from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from libs.strategy.base import DecisionEnvelope, Freshness, OrderStyle, StrategyAlgorithm, StrategyParams
from strategies.options_plugins.qqq_1dte_ma10_reject import QQQ1DTEMa10RejectPlugin
from strategies.options_plugins.qqq_0dte_opt_live import QQQ0DTEOptionsLivePlugin
from strategies.options_plugins.spy_1dte_ma10_reject import SPY1DTEMa10RejectPlugin
from strategies.options_kernel.base import OptionKernelStrategy
from strategies.options_kernel.plugin import BaseOptionPlugin
from strategies.options_kernel.types import (
    EntryPlan,
    ExecutionProfile,
    ExitPlan,
    IBKRTrailingStopOrderSpec,
    KernelBarContext,
    KernelIntentRecord,
    KernelPositionState,
    PluginRuntimeState,
    Selection,
    Signal,
    UnderlyingTriggeredOptionExitPlan,
)
from strategies.qqq_1dte_ma10_reject_pure_strategy import QQQ1DTEMA10RejectPureStrategy
from strategies.qqq_0dte_opt_live_pure_strategy import QQQ0DTEOptionsLivePureStrategy
from strategies.spy_1dte_ma10_reject_pure_strategy import SPY1DTEMA10RejectPureStrategy


class _DummyDelegate(StrategyAlgorithm):
    async def initialize(self) -> None:
        return None

    async def on_features(self, symbol: str, features: dict) -> None:
        if not features.get("go"):
            return
        env = DecisionEnvelope(
            decision_id="d1",
            strategy_id=self.ctx.strategy_id,
            symbol="OPT:QQQ:20260326:590:P",
            ts_bar_end_us=int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000),
            target_qty=4.0,
            order_style=OrderStyle(kind="MKT"),
            freshness=Freshness(ttl_ms=1000, dlb_ms=500),
            extras={"order_ref": "oref1"},
        )
        await self.ctx.submit_intent(env)


class _DummyPlugin(BaseOptionPlugin):
    component_name = "dummy_plugin"

    def __init__(self):
        self.calls = {"signal": 0, "entry": 0, "select": 0, "profile": 0, "exit": 0}

    def build_delegate(self, ctx, params):
        return _DummyDelegate(ctx, params)

    def build_signal(self, strategy, ctx):
        self.calls["signal"] += 1
        if not ctx.get("features", {}).get("go"):
            return None
        return Signal(ts=ctx["now"], side="PUT", signal_kind="dummy")

    def build_entry_plan(self, strategy, ctx, signal):
        self.calls["entry"] += 1
        return EntryPlan(
            ts=ctx["now"],
            side=signal.side,
            signal_kind=signal.signal_kind,
            structure_stop=1.0,
            target_1r=2.0,
            target_1p2r=3.0,
            r_per_share=1.0,
            contracts=1,
        )

    def select_contract(self, strategy, ctx, entry_plan):
        self.calls["select"] += 1
        return Selection(
            opt_symbol="OPT:QQQ:20260326:590:P",
            expiry="20260326",
            strike=590.0,
            side=entry_plan.side,
            bid=1.0,
            ask=1.1,
            mid=1.05,
        )

    def build_execution_profile(self, strategy, ctx, entry_plan, selection):
        self.calls["profile"] += 1
        return ExecutionProfile(
            profile_id="dummy_profile",
            market_data_profile_id="dummy_md",
            timeout_bars=2,
        )

    def build_exit_plan(self, strategy, ctx, market_state):
        self.calls["exit"] += 1
        return None


class _DummyKernelStrategy(OptionKernelStrategy):
    plugin_cls = _DummyPlugin


class _KernelSubmitDelegate(StrategyAlgorithm):
    async def initialize(self) -> None:
        return None

    async def on_features(self, symbol: str, features: dict) -> None:
        return None


class _KernelSubmitPlugin(BaseOptionPlugin):
    component_name = "kernel_submit_plugin"
    kernel_can_submit = True

    def build_delegate(self, ctx, params):
        return _KernelSubmitDelegate(ctx, params)

    def build_signal(self, strategy, ctx):
        if not ctx.get("features", {}).get("go"):
            return None
        return Signal(ts=ctx["now"], side="CALL", signal_kind="kernel_signal")

    def build_entry_plan(self, strategy, ctx, signal):
        return EntryPlan(
            ts=ctx["now"],
            side=signal.side,
            signal_kind=signal.signal_kind,
            structure_stop=0.0,
            target_1r=0.0,
            target_1p2r=0.0,
            r_per_share=0.0,
            contracts=2,
        )

    def select_contract(self, strategy, ctx, entry_plan):
        return Selection(
            opt_symbol="OPT:QQQ:20260326:600:C",
            expiry="20260326",
            strike=600.0,
            side=entry_plan.side,
            bid=1.0,
            ask=1.1,
            mid=1.05,
        )

    def build_execution_profile(self, strategy, ctx, entry_plan, selection):
        return ExecutionProfile(profile_id="kernel_submit", market_data_profile_id="md", timeout_bars=2)


class _KernelSubmitStrategy(OptionKernelStrategy):
    plugin_cls = _KernelSubmitPlugin


class _PurePlugin(BaseOptionPlugin):
    component_name = "pure_plugin"
    supports_pure_kernel = True

    def build_execution_profile(self, strategy, ctx, entry_plan, selection):
        return ExecutionProfile(profile_id="unused", market_data_profile_id="unused", timeout_bars=1)

    def build_signal_pure(self, runtime):
        if runtime.symbol != "QQQ":
            return None
        return Signal(ts=runtime.now, side="PUT", signal_kind="pure_signal")

    def build_entry_plan_pure(self, runtime, signal):
        return EntryPlan(
            ts=runtime.now,
            side=signal.side,
            signal_kind=signal.signal_kind,
            structure_stop=1.0,
            target_1r=2.0,
            target_1p2r=3.0,
            r_per_share=1.0,
            contracts=3,
            payload={"runtime_symbol": runtime.symbol},
        )

    def select_contract_pure(self, runtime, entry_plan):
        return Selection(
            opt_symbol="OPT:QQQ:20260326:585:P",
            expiry="20260326",
            strike=585.0,
            side=entry_plan.side,
            bid=1.2,
            ask=1.3,
            mid=1.25,
        )

    def build_execution_profile_pure(self, runtime, entry_plan, selection):
        return ExecutionProfile(profile_id="pure_profile", market_data_profile_id="pure_md", timeout_bars=3)


class _PureKernelStrategy(OptionKernelStrategy):
    plugin_cls = _PurePlugin


class _PureRoundTripPlugin(BaseOptionPlugin):
    component_name = "pure_round_trip"
    supports_pure_kernel = True

    def __init__(self):
        self._entered = False

    def build_signal_pure(self, runtime):
        if self._entered or runtime.active_position is not None:
            return None
        return Signal(ts=runtime.now, side="CALL", signal_kind="round_trip")

    def build_entry_plan_pure(self, runtime, signal):
        self._entered = True
        return EntryPlan(
            ts=runtime.now,
            side=signal.side,
            signal_kind=signal.signal_kind,
            structure_stop=1.5,
            target_1r=2.5,
            target_1p2r=3.0,
            r_per_share=1.0,
            contracts=2,
        )

    def select_contract_pure(self, runtime, entry_plan):
        return Selection(
            opt_symbol="OPT:QQQ:20260326:600:C",
            expiry="20260326",
            strike=600.0,
            side=entry_plan.side,
            bid=1.0,
            ask=1.1,
            mid=1.05,
        )

    def build_execution_profile_pure(self, runtime, entry_plan, selection):
        return ExecutionProfile(profile_id="round_trip", market_data_profile_id="pure_md", timeout_bars=3)

    def build_exit_plan_pure(self, runtime):
        if runtime.active_position is None:
            return None
        return ExitPlan(
            ts=runtime.now,
            position_lot_id=runtime.active_position.position_lot_id,
            qty=runtime.active_position.remaining_qty,
            reason="PURE_EXIT",
        )


class _PureRoundTripStrategy(OptionKernelStrategy):
    plugin_cls = _PureRoundTripPlugin


class _LegacyOverridePlugin(_PurePlugin):
    component_name = "legacy_override_plugin"

    def __init__(self):
        super().__init__()
        self.order_submitted_events = []
        self.legacy_calls = 0

    def on_order_submitted(self, *, runtime, ack, signal=None, entry_plan=None, exit_plan=None, selection=None):
        self.order_submitted_events.append(ack)

    def on_entry_submitted_pure(self, *, runtime, signal, entry_plan, selection, position_lot_id):
        self.legacy_calls += 1
        return None


class _LegacyOverrideStrategy(OptionKernelStrategy):
    plugin_cls = _LegacyOverridePlugin


class _PureMultiExitPlugin(BaseOptionPlugin):
    component_name = "pure_multi_exit"
    supports_pure_kernel = True

    def build_signal_pure(self, runtime):
        return None

    def build_execution_profile_pure(self, runtime, entry_plan, selection):
        return ExecutionProfile(profile_id="unused", market_data_profile_id="unused", timeout_bars=1)

    def build_exit_plans_pure(self, runtime):
        if len(runtime.active_positions) < 2:
            return []
        return [
            ExitPlan(
                ts=runtime.now,
                position_lot_id=runtime.active_positions[0].position_lot_id,
                qty=runtime.active_positions[0].remaining_qty,
                reason="EXIT_A",
            ),
            ExitPlan(
                ts=runtime.now,
                position_lot_id=runtime.active_positions[1].position_lot_id,
                qty=runtime.active_positions[1].remaining_qty,
                reason="EXIT_B",
            ),
        ]


class _PureMultiExitStrategy(OptionKernelStrategy):
    plugin_cls = _PureMultiExitPlugin


class _AuditWriterCapture:
    def __init__(self):
        self.rows = []

    def emit(self, **kwargs):
        self.rows.append(dict(kwargs))


def _make_ctx():
    ctx = MagicMock()
    ctx.strategy_id = "dummy_strategy_v2"
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.system_health = SimpleNamespace(subscribe=AsyncMock())
    ctx.submit_intent = AsyncMock()
    ctx.subscribe_features = AsyncMock()
    ctx.history = AsyncMock(return_value=[])
    ctx.portfolio = AsyncMock(return_value={"positions": []})
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    return ctx


def _make_runtime_for_exit(*, now, history, pos):
    return PluginRuntimeState(
        strategy_id="qqq_1dte_pure_test",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=float(history[-1]["close"]),
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=float(history[-1]["open"]),
            high=float(history[-1]["high"]),
            low=float(history[-1]["low"]),
            close=float(history[-1]["close"]),
            volume=float(history[-1].get("volume", 1000.0)),
            vwap=float(history[-1].get("vwap", history[-1]["close"])),
            atr=float(history[-1].get("atr", 1.0)),
            payload=dict(history[-1]),
        ),
        active_position=pos,
        payload={"bar_history": history},
    )


def test_option_kernel_idle_heartbeat_is_not_entry_tradable():
    ctx = _make_ctx()
    params = StrategyParams(extras={"underlying": "QQQ", "history_ready_min_bars": 2})
    strat = _DummyKernelStrategy(ctx, params)
    strat.kernel._bar_history = [{"close": 1.0}, {"close": 1.1}]

    tradable, blockers, payload = strat._strategy_tradability_payload(source="option_kernel_idle_heartbeat")

    assert tradable is False
    assert blockers == ["idle_not_in_entry_window"]
    assert payload["readiness_state"] == "idle_not_in_entry_window"
    assert payload["tradable_for_entry"] is False
    assert payload["data_health_ok"] is True
    assert payload["tradable"] is False


def test_option_kernel_entry_heartbeat_requires_warmup():
    ctx = _make_ctx()
    params = StrategyParams(
        extras={
            "underlying": "QQQ",
            "ab_time_start": "00:00:00",
            "no_new_entry_after": "23:59:00",
            "history_ready_min_bars": 2,
        }
    )
    strat = _DummyKernelStrategy(ctx, params)
    strat.kernel._bar_history = [{"close": 1.0}]

    tradable, blockers, payload = strat._strategy_tradability_payload(source="option_kernel_feature_heartbeat")

    assert tradable is False
    assert blockers == ["insufficient_warmup"]
    assert payload["readiness_state"] == "entry_blocked"
    assert payload["tradable_for_entry"] is False
    assert payload["data_health_ok"] is False
    assert payload["data_blockers"] == ["insufficient_warmup"]


@pytest.mark.asyncio
async def test_option_kernel_records_owned_lot_on_submit():
    ctx = _make_ctx()
    params = StrategyParams(extras={"live_parity_profile_id": "prof1"})
    strat = _DummyKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"go": True})

    ctx.submit_intent.assert_awaited_once()
    snapshot = strat.kernel.state_snapshot()
    assert snapshot["execution_profile"]["profile_id"] == "prof1"
    assert len(snapshot["ledger"]["lots"]) == 1
    lot = snapshot["ledger"]["lots"][0]
    assert lot["strategy_id"] == "dummy_strategy_v2"
    assert lot["position_lot_id"]


@pytest.mark.asyncio
async def test_option_kernel_does_not_submit_when_delegate_has_no_signal():
    ctx = _make_ctx()
    params = StrategyParams()
    strat = _DummyKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"go": False})

    ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_kernel_hybrid_mode_runs_plugin_lifecycle_snapshot():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "hybrid"})
    strat = _DummyKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"go": True, "close": 100.0, "ts": datetime(2026, 3, 25, 10, 0, 0)})

    snapshot = strat.kernel.state_snapshot()
    assert snapshot["kernel_mode"] == "hybrid"
    assert snapshot["plugin_lifecycle"]["symbol"] == "QQQ"
    assert snapshot["plugin_lifecycle"]["signal"]["signal_kind"] == "dummy"
    assert snapshot["execution_profile"]["profile_id"] == "dummy_profile"
    assert strat.plugin.calls["signal"] >= 1
    assert strat.plugin.calls["entry"] >= 1
    assert strat.plugin.calls["select"] >= 1
    assert strat.plugin.calls["profile"] >= 1


@pytest.mark.asyncio
async def test_option_kernel_strict_scope_validation_blocks_missing_strategy_id():
    ctx = _make_ctx()
    params = StrategyParams(
        extras={
            "strict_scope_validation": True,
            "shared_cap_group_strategy_ids": ["some_other_strategy"],
        }
    )
    with pytest.raises(ValueError):
        _DummyKernelStrategy(ctx, params)


@pytest.mark.asyncio
async def test_option_kernel_plugin_mode_submits_intent_without_delegate_execution():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "plugin"})
    strat = _KernelSubmitStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"go": True, "close": 100.0, "ts": datetime(2026, 3, 25, 10, 1, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert env.symbol == "OPT:QQQ:20260326:600:C"
    assert float(env.target_qty) == 2.0
    assert str((env.extras or {}).get("component_name")) == "kernel_submit_plugin"


@pytest.mark.asyncio
async def test_option_kernel_plugin_mode_captures_delegate_intent_and_replays_via_kernel():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "plugin"})
    strat = _DummyKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"go": True, "close": 100.0, "ts": datetime(2026, 3, 25, 10, 2, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert env.symbol == "OPT:QQQ:20260326:590:P"
    assert str((env.extras or {}).get("captured_from_delegate")) == "True" or bool((env.extras or {}).get("captured_from_delegate")) is True
    snapshot = strat.kernel.state_snapshot()
    assert snapshot["kernel_mode"] == "plugin"


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_mode_submits_without_delegate():
    ctx = _make_ctx()
    writer = _AuditWriterCapture()
    ctx.engine = SimpleNamespace(audit_writer=writer)
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert env.symbol == "OPT:QQQ:20260326:585:P"
    assert float(env.target_qty) == 3.0
    extras = dict(env.extras or {})
    assert extras["entry_intent_schema"] == "EntryIntent/v1"
    assert extras["entry_intent"]["option_symbol"] == "OPT:QQQ:20260326:585:P"
    assert extras["entry_intent"]["qty"] == pytest.approx(3.0)
    assert extras["entry_intent"]["position_lot_id"] == extras["position_id"]
    snapshot = strat.kernel.state_snapshot()
    assert snapshot["kernel_mode"] == "pure_plugin"
    assert snapshot["plugin_lifecycle"]["pure_plugin"] is True
    assert snapshot["execution_profile"]["profile_id"] == "pure_profile"
    rows = [row for row in writer.rows if row.get("stage") == "plugin_cycle_observed"]
    assert rows
    assert rows[-1]["strategy_id"] == "dummy_strategy_v2"
    assert rows[-1]["status"] == "signal"
    assert rows[-1]["payload"]["has_signal"] is True
    assert rows[-1]["payload"]["opt_symbol"] == "OPT:QQQ:20260326:585:P"


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_no_signal_still_emits_cycle_audit():
    class _NoSignalPlugin(BaseOptionPlugin):
        component_name = "no_signal_plugin"
        supports_pure_kernel = True

        def build_signal_pure(self, runtime):
            return None

    class _NoSignalStrategy(OptionKernelStrategy):
        plugin_cls = _NoSignalPlugin

    ctx = _make_ctx()
    writer = _AuditWriterCapture()
    ctx.engine = SimpleNamespace(audit_writer=writer)
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _NoSignalStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 4, 0)})

    ctx.submit_intent.assert_not_awaited()
    rows = [row for row in writer.rows if row.get("stage") == "plugin_cycle_observed"]
    assert rows
    assert rows[-1]["status"] == "observed"
    assert rows[-1]["reason"] == "no_signal"
    assert rows[-1]["payload"]["has_signal"] is False


@pytest.mark.asyncio
async def test_option_kernel_entry_envelope_carries_entry_signal_score_from_extras():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "entry_signal_score": 0.85})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert float(env.signal_score) == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_option_kernel_entry_envelope_defaults_signal_score_to_half_without_config():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert float(env.signal_score) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_option_kernel_entry_envelope_carries_edge_bp_and_derives_signal_score():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "entry_edge_bp": 90.0})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert float(env.signal_score) == pytest.approx(0.9)
    assert float((env.extras or {}).get("edge_bp")) == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_option_kernel_standard_order_submitted_hook_survives_legacy_override():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _LegacyOverrideStrategy(ctx, params)
    await strat.initialize()

    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    assert strat.plugin.legacy_calls == 1
    assert len(strat.plugin.order_submitted_events) == 1
    ack = strat.plugin.order_submitted_events[0]
    assert ack.intent_action == "entry"
    assert ack.symbol == "OPT:QQQ:20260326:585:P"
    assert ack.position_lot_id


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_mode_can_size_contracts_by_entry_notional():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "entry_notional_usd": 10000})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    assert env.symbol == "OPT:QQQ:20260326:585:P"
    # selection mid=1.25 -> per-contract notional=125 -> floor(10000/125)=80
    assert float(env.target_qty) == 80.0


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_exit_marks_close_only_option_context():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "underlying": "QQQ"})
    strat = _PureRoundTripStrategy(ctx, params)
    await strat.initialize()

    opened_at = datetime(2026, 3, 25, 10, 4, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d_exit_1",
            symbol="OPT:QQQ:20260326:600:C",
            target_qty=2.0,
            ts_bar_end_us=int(opened_at.timestamp() * 1_000_000),
            order_ref="oref-exit-1",
            position_lot_id="lot_exit_1",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "CALL", "initial_qty": 2.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot_exit_1", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot_exit_1", filled_qty=2.0)

    history = [
        {
            "ts": opened_at,
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 1000.0,
            "vwap": 100.4,
            "atr": 1.0,
        }
    ]
    runtime = _make_runtime_for_exit(
        now=opened_at.replace(minute=5),
        history=history,
        pos=strat.kernel._build_runtime_state("QQQ", {"close": 100.5, "ts": opened_at}).active_position,
    )
    assert runtime.active_position is not None

    exit_plan = strat.plugin.build_exit_plan_pure(runtime)
    assert exit_plan is not None

    await strat.kernel._submit_plugin_exit_intent(
        ctx={"now": opened_at.replace(minute=5)},
        exit_plan=exit_plan,
    )

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    extras = dict(env.extras or {})
    assert float(env.target_qty) < 0.0
    assert extras["intent_action"] == "exit"
    assert extras["instrument_type"] == "OPTION"
    assert extras["underlying"] == "QQQ"
    assert extras["bars_symbol"] == "QQQ"
    assert extras["close_only"] is True
    assert extras["flatten_action"] is False
    assert extras["position_qty_hint"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_option_kernel_exit_intent_carries_underlying_trigger_payload():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "underlying": "QQQ"})
    strat = _PureRoundTripStrategy(ctx, params)
    await strat.initialize()

    opened_at = datetime(2026, 3, 25, 10, 4, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d_exit_underlying",
            symbol="OPT:QQQ:20260326:600:C",
            target_qty=2.0,
            ts_bar_end_us=int(opened_at.timestamp() * 1_000_000),
            order_ref="oref-exit-underlying",
            position_lot_id="lot_exit_underlying",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "CALL", "initial_qty": 2.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot_exit_underlying", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot_exit_underlying", filled_qty=2.0)
    exit_plan = UnderlyingTriggeredOptionExitPlan(
        underlying_symbol="QQQ",
        trigger_price=499.25,
        trigger_direction="BELOW",
        option_contract_id="OPT:QQQ:20260326:600:C",
        exit_action="SELL",
        quantity=2,
        fallback_order_type="MARKET",
        ibkr_trailing_order_spec=IBKRTrailingStopOrderSpec(
            action="SELL",
            orderType="TRAIL",
            totalQuantity=2,
            trailingPercent=12.5,
        ),
        option_stop_order_spec={"orderType": "STP", "auxPrice": 1.0},
    ).to_exit_plan(ts=opened_at.replace(minute=5), position_lot_id="lot_exit_underlying")

    await strat.kernel._submit_plugin_exit_intent(ctx={"now": opened_at.replace(minute=5)}, exit_plan=exit_plan)

    env = ctx.submit_intent.await_args.args[0]
    extras = dict(env.extras or {})
    assert extras["intent_action"] == "exit"
    assert extras["exit_model"] == "UNDERLYING_TRIGGERED_OPTION_EXIT"
    assert extras["underlying_trigger"]["trigger_price"] == pytest.approx(499.25)
    assert extras["ibkr_trailing_order_spec"]["orderType"] == "TRAIL"
    assert extras["option_stop_order_spec"]["auxPrice"] == pytest.approx(1.0)
    assert extras["exit_plan_payload"]["ibkr_trailing_order_spec"]["trailingPercent"] == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_eod_flatten_sets_flatten_action_and_hint():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "underlying": "QQQ"})
    strat = _PureRoundTripStrategy(ctx, params)
    await strat.initialize()

    opened_at = datetime(2026, 3, 25, 15, 49, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d_exit_eod",
            symbol="OPT:QQQ:20260326:600:C",
            target_qty=2.0,
            ts_bar_end_us=int(opened_at.timestamp() * 1_000_000),
            order_ref="oref-exit-eod",
            position_lot_id="lot_exit_eod",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "CALL", "initial_qty": 2.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot_exit_eod", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot_exit_eod", filled_qty=2.0)

    await strat.kernel._submit_plugin_exit_intent(
        ctx={"now": opened_at.replace(minute=50)},
        exit_plan=ExitPlan(
            ts=opened_at.replace(minute=50),
            position_lot_id="lot_exit_eod",
            qty=2.0,
            reason="EOD_FORCE_FLATTEN",
        ),
    )

    env = ctx.submit_intent.await_args.args[0]
    extras = dict(env.extras or {})
    assert extras["close_only"] is True
    assert extras["flatten_action"] is True
    assert extras["position_qty_hint"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_mode_propagates_definition_namespaces():
    ctx = _make_ctx()
    params = StrategyParams(
        extras={
            "kernel_mode": "pure_plugin",
            "definition_id": "TEST_DEF",
            "audit_namespace": "audit.test_def",
            "position_namespace": "pos.test_def",
            "risk_symbol": "OPT:QQQ:TEST",
        }
    )
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()
    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})

    ctx.submit_intent.assert_awaited_once()
    env = ctx.submit_intent.await_args.args[0]
    extras = dict(env.extras or {})
    assert extras["definition_id"] == "TEST_DEF"
    assert extras["audit_namespace"] == "audit.test_def"
    assert extras["position_namespace"] == "pos.test_def"
    assert extras["risk_symbol"] == "OPT:QQQ:TEST"
    assert str(extras["position_id"]).startswith("lot:pos.test_def:")
    assert str(extras["order_ref"]).startswith("optk:pos.test_def:")
    snapshot = strat.kernel.state_snapshot()
    assert snapshot["definition_id"] == "TEST_DEF"
    assert snapshot["audit_namespace"] == "audit.test_def"
    assert snapshot["position_namespace"] == "pos.test_def"
    assert snapshot["risk_symbol"] == "OPT:QQQ:TEST"


def test_option_kernel_runtime_state_exposes_all_active_lots():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    now = datetime(2026, 3, 25, 10, 3, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d1",
            symbol="OPT:QQQ:20260326:585:P",
            target_qty=1.0,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            order_ref="oref1",
            position_lot_id="lot1",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "PUT", "initial_qty": 1.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot1", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot1", filled_qty=1.0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d2",
            symbol="OPT:QQQ:20260326:580:P",
            target_qty=2.0,
            ts_bar_end_us=int(now.replace(minute=4).timestamp() * 1_000_000),
            order_ref="oref2",
            position_lot_id="lot2",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "PUT", "initial_qty": 2.0, "entry_mid": 1.4},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot2", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot2", filled_qty=2.0)
    runtime = strat.kernel._build_runtime_state("QQQ", {"close": 100.0, "ts": now})
    assert runtime.active_position is not None
    assert len(runtime.active_positions) == 2
    assert {row.position_lot_id for row in runtime.active_positions} == {"lot1", "lot2"}
    assert runtime.active_position.position_lot_id in {"lot1", "lot2"}


@pytest.mark.asyncio
async def test_option_kernel_pending_entry_does_not_become_active_until_portfolio_sync():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    now = datetime(2026, 3, 25, 10, 3, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d1",
            symbol="OPT:QQQ:20260326:585:P",
            target_qty=1.0,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            order_ref="oref1",
            position_lot_id="lot1",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "PUT", "initial_qty": 1.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot1", accepted=True)

    runtime = strat.kernel._build_runtime_state("QQQ", {"close": 100.0, "ts": now})
    assert runtime.active_position is None
    assert runtime.payload["pending_entry_count"] == 1

    ctx.portfolio.return_value = {
        "positions": [
            {
                "symbol": "OPT:QQQ:20260326:585:P",
                "qty": 1.0,
                "avg_px": 1.23,
            }
        ]
    }
    await strat.kernel._sync_pending_entry_lots_from_portfolio()
    runtime2 = strat.kernel._build_runtime_state("QQQ", {"close": 100.0, "ts": now})
    assert runtime2.active_position is not None
    assert runtime2.active_position.position_lot_id == "lot1"
    assert runtime2.active_position.avg_entry_px == pytest.approx(1.23)
    assert runtime2.payload["pending_entry_count"] == 0


@pytest.mark.asyncio
async def test_option_kernel_reconciles_active_lot_to_flat_when_portfolio_is_flat():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    now = datetime(2026, 3, 25, 10, 3, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d1",
            symbol="OPT:QQQ:20260326:585:P",
            target_qty=1.0,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            order_ref="oref1",
            position_lot_id="lot1",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "PUT", "initial_qty": 1.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot1", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot1", filled_qty=1.0)

    ctx.portfolio.return_value = {"positions": []}
    await strat.kernel._sync_pending_entry_lots_from_portfolio()
    runtime = strat.kernel._build_runtime_state("QQQ", {"close": 100.0, "ts": now})
    assert runtime.active_position is None
    assert runtime.active_positions == []


@pytest.mark.asyncio
async def test_option_kernel_blocks_duplicate_submit_while_pending_entry_open():
    ctx = _make_ctx()
    ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "stage": "submitted", "reasons": []})
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()

    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 0)})
    assert ctx.submit_intent.await_count == 1

    await strat.on_features("QQQ", {"close": 100.1, "ts": datetime(2026, 3, 25, 10, 4, 0)})
    assert ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_option_kernel_allows_retry_after_rejected_submit_in_same_minute():
    ctx = _make_ctx()
    ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "stage": "broker", "reasons": ["gateway_busy"]})
    params = StrategyParams(
        extras={
            "kernel_mode": "pure_plugin",
            "entry_reject_cooldown_sec": 0.0,
            "entry_reject_cooldown_deterministic_sec": 0.0,
        }
    )
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()

    ts = datetime(2026, 3, 25, 10, 3, 5)
    await strat.on_features("QQQ", {"close": 100.0, "ts": ts})
    await strat.on_features("QQQ", {"close": 100.1, "ts": datetime(2026, 3, 25, 10, 3, 40)})

    assert ctx.submit_intent.await_count == 2


@pytest.mark.asyncio
async def test_option_kernel_deterministic_reject_locks_same_minute_even_if_base_cooldown_zero():
    ctx = _make_ctx()
    ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "stage": "validator", "reasons": ["no_nbbo"]})
    params = StrategyParams(
        extras={
            "kernel_mode": "pure_plugin",
            "entry_reject_cooldown_sec": 0.0,
            "entry_reject_cooldown_deterministic_sec": 15.0,
        }
    )
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()

    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 5)})
    await strat.on_features("QQQ", {"close": 100.1, "ts": datetime(2026, 3, 25, 10, 3, 40)})

    assert ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_option_kernel_reject_cooldown_blocks_rapid_same_minute_retry():
    ctx = _make_ctx()
    ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "stage": "validator", "reasons": ["no_nbbo"]})
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "entry_reject_cooldown_sec": 60.0})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()

    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 3, 5)})
    await strat.on_features("QQQ", {"close": 100.1, "ts": datetime(2026, 3, 25, 10, 3, 15)})

    assert ctx.submit_intent.await_count == 1


def test_qqq_1dte_plugin_supports_pure_kernel_defaults():
    plugin = QQQ1DTEMa10RejectPlugin()
    extras = plugin.apply_default_extras({})
    assert plugin.supports_pure_kernel is True
    assert extras["kernel_mode"] == "pure_plugin"
    assert extras["live_parity_profile_id"] == "qqq_option_ma_ext_v1"
    assert extras["definition_id"] == "QQQ_MA_CROSS_STACK_CONT_V2_DUAL"


def test_spy_1dte_plugin_uses_dual_definition_with_spy_defaults():
    plugin = SPY1DTEMa10RejectPlugin()
    extras = plugin.apply_default_extras({})
    assert plugin.supports_pure_kernel is True
    assert extras["underlying"] == "SPY"
    assert extras["risk_symbol"] == "OPT:SPY:1DTE"
    assert extras["live_parity_profile_id"] == "spy_option_ma_ext_v1"
    assert extras["definition_id"] == "SPY_MA_CROSS_STACK_CONT_V2_DUAL"
    assert extras["replay_strategy_id"] == "ma_cross_stack_cont_v2_dual_exp"


def test_spy_1dte_plugin_copies_pending_setup_from_dual_signal_module():
    plugin = SPY1DTEMa10RejectPlugin()
    plugin.apply_default_extras({})
    now = datetime(2026, 3, 27, 10, 0, 0)
    pending = SimpleNamespace(side="CALL", signal_kind="dual_breakout", regime="BULL", setup_idx=219, setup_ts=now)

    class _SignalModule:
        def __init__(self):
            self._pending_setup = pending
            self._entry_plan = None
            self.last_diag = {"dual_call_side": "CALL", "dual_call_trigger": "breakout_cont_call"}
            self.last_block = ("setup", "SETUP_READY_WAIT_CONFIRM")

        def build_signal(self, _ctx):
            return None

    plugin._strategy_definition = SimpleNamespace(signal_module=_SignalModule())
    plugin._pure_last_day = now.date()

    runtime = PluginRuntimeState(
        strategy_id="spy_test",
        kernel_mode="pure_plugin",
        symbol="SPY",
        now=now,
        wall_now=now,
        spot=590.2,
        bar=KernelBarContext(ts=now, symbol="SPY", close=590.2),
        payload={"bar_history": [{"ts": now}]},
    )
    out = plugin.build_signal_pure(runtime)
    assert out is None
    assert plugin._pure_pending_setup is not None
    assert str(plugin._pure_pending_setup.signal_kind) == "dual_breakout"


def test_spy_1dte_plugin_returns_signal_when_dual_module_confirms():
    plugin = SPY1DTEMa10RejectPlugin()
    plugin.apply_default_extras({})
    now = datetime(2026, 3, 27, 10, 1, 0)
    signal = Signal(ts=now, side="PUT", signal_kind="breakdown_cont_put", grade="A", regime="BEAR", payload={"close": 589.0})

    class _SignalModule:
        def __init__(self):
            self._pending_setup = None
            self._entry_plan = {"r_per_share": 1.2}
            self.last_diag = {"dual_put_side": "PUT", "dual_put_trigger": "breakdown_cont_put"}
            self.last_block = ("signal", "SIGNAL_READY")

        def build_signal(self, _ctx):
            return signal

    plugin._strategy_definition = SimpleNamespace(signal_module=_SignalModule())
    plugin._pure_last_day = now.date()
    runtime = PluginRuntimeState(
        strategy_id="spy_test",
        kernel_mode="pure_plugin",
        symbol="SPY",
        now=now,
        wall_now=now,
        spot=589.0,
        bar=KernelBarContext(ts=now, symbol="SPY", close=589.0),
        payload={"bar_history": [{"ts": now}]},
    )
    out = plugin.build_signal_pure(runtime)
    assert out is signal
    assert plugin._pure_pending_setup is None
    assert float(plugin._pure_entry_plan["r_per_share"]) == 1.2


@pytest.mark.asyncio
async def test_real_1dte_pure_strategy_entrypoints_initialize_without_delegate():
    qqq_ctx = _make_ctx()
    spy_ctx = _make_ctx()
    qqq_ctx.strategy_id = "qqq_1dte_pure_test"
    spy_ctx.strategy_id = "spy_1dte_pure_test"
    qqq = QQQ1DTEMA10RejectPureStrategy(qqq_ctx, StrategyParams(extras={"kernel_mode": "pure_plugin"}))
    spy = SPY1DTEMA10RejectPureStrategy(spy_ctx, StrategyParams(extras={"kernel_mode": "pure_plugin"}))

    await qqq.initialize()
    await spy.initialize()

    assert qqq.kernel.delegate is None
    assert spy.kernel.delegate is None
    assert qqq.kernel.kernel_mode == "pure_plugin"
    assert spy.kernel.kernel_mode == "pure_plugin"
    qqq_ctx.subscribe_features.assert_awaited_once_with(["QQQ"], bar_sec=60)
    spy_ctx.subscribe_features.assert_awaited_once_with(["SPY"], bar_sec=60)


@pytest.mark.asyncio
async def test_real_0dte_pure_strategy_entrypoint_initializes_without_delegate():
    ctx = _make_ctx()
    ctx.strategy_id = "qqq_0dte_pure_test"
    strat = QQQ0DTEOptionsLivePureStrategy(ctx, StrategyParams(extras={"kernel_mode": "pure_plugin"}))
    await strat.initialize()
    assert strat.kernel.delegate is None
    assert strat.kernel.kernel_mode == "pure_plugin"
    ctx.subscribe_features.assert_awaited_once_with(["QQQ"], bar_sec=5)


@pytest.mark.asyncio
async def test_real_1dte_pure_strategy_preloads_history_into_kernel_bar_history():
    ctx = _make_ctx()
    ctx.strategy_id = "qqq_1dte_pure_preload_test"
    preload_rows = [
        {
            "ts": datetime(2026, 3, 27, 9, 28, 0),
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "vwap": 100.4,
            "volume": 1200.0,
        },
        {
            "ts": datetime(2026, 3, 27, 9, 29, 0),
            "open": 100.5,
            "high": 101.2,
            "low": 100.2,
            "close": 101.0,
            "vwap": 100.9,
            "volume": 1400.0,
        },
    ]
    ctx.history = AsyncMock(return_value=preload_rows)
    strat = QQQ1DTEMA10RejectPureStrategy(
        ctx,
        StrategyParams(extras={"kernel_mode": "pure_plugin", "preload_history_bars": 2}),
    )

    await strat.initialize()

    snapshot = strat.kernel.state_snapshot()
    assert snapshot["bar_history_count"] == 2
    assert strat.kernel._bar_history[-1]["close"] == 101.0


def test_qqq_0dte_plugin_supports_pure_kernel_defaults():
    plugin = QQQ0DTEOptionsLivePlugin()
    extras = plugin.apply_default_extras({})
    assert plugin.supports_pure_kernel is True
    assert extras["kernel_mode"] == "hybrid"
    assert extras["dte_preferred"] == 0


def test_qqq_0dte_pure_entry_count_advances_only_after_submit():
    plugin = QQQ0DTEOptionsLivePlugin()
    plugin.apply_default_extras({})
    runtime = PluginRuntimeState(
        strategy_id="qqq_0dte_pure_test",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=datetime(2026, 3, 25, 10, 0, 0),
        wall_now=datetime(2026, 3, 25, 10, 0, 0),
        spot=100.0,
        bar=KernelBarContext(ts=datetime(2026, 3, 25, 10, 0, 0), symbol="QQQ", close=100.0),
        payload={},
    )
    plugin._pure_current_completed_bar = {"close": 100.0, "vwap": 99.9, "atr_1m": 0.2}
    signal = Signal(ts=runtime.now, side="CALL", signal_kind="qqq_0dte_confirm")
    plan = plugin.build_entry_plan_pure(runtime, signal)
    assert plan is not None
    assert plugin._pure_trades_filled_today == 0
    plugin.on_entry_submitted_pure(
        runtime=runtime,
        signal=signal,
        entry_plan=plan,
        selection=Selection(
            opt_symbol="OPT:QQQ:20260326:500:C",
            expiry="20260326",
            strike=500.0,
            side="CALL",
            bid=1.0,
            ask=1.1,
            mid=1.05,
        ),
        position_lot_id="lot:test",
    )
    assert plugin._pure_trades_filled_today == 1


@pytest.mark.asyncio
async def test_option_kernel_emits_state_audit_for_entry_and_live_sync():
    ctx = _make_ctx()
    ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "stage": "submitted", "reasons": []})
    writer = _AuditWriterCapture()
    ctx.engine = SimpleNamespace(audit_writer=writer)
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureKernelStrategy(ctx, params)
    await strat.initialize()

    now = datetime(2026, 3, 25, 10, 7, 0)
    await strat.on_features("QQQ", {"close": 100.0, "ts": now})
    ctx.portfolio.return_value = {"positions": [{"symbol": "OPT:QQQ:20260326:585:P", "qty": 3.0, "avg_px": 1.25}]}
    await strat.kernel._sync_pending_entry_lots_from_portfolio()

    state_rows = [row for row in writer.rows if str(row.get("domain")) == "state"]
    pairs = {(str(row.get("stage")), str(row.get("status"))) for row in state_rows}
    assert ("runtime_transition", "intent_submitted") in pairs
    assert ("order_transition", "submitted") in pairs
    assert ("runtime_transition", "live") in pairs
    assert ("protection_transition", "protected") in pairs


@pytest.mark.asyncio
async def test_option_kernel_emits_flatten_submitted_and_confirmed_for_exit():
    ctx = _make_ctx()
    ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "stage": "submitted", "reasons": []})
    writer = _AuditWriterCapture()
    ctx.engine = SimpleNamespace(audit_writer=writer)
    params = StrategyParams(extras={"kernel_mode": "pure_plugin", "underlying": "QQQ"})
    strat = _PureRoundTripStrategy(ctx, params)
    await strat.initialize()

    opened_at = datetime(2026, 3, 25, 10, 8, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d_exit_state_1",
            symbol="OPT:QQQ:20260326:600:C",
            target_qty=2.0,
            ts_bar_end_us=int(opened_at.timestamp() * 1_000_000),
            order_ref="oref-exit-state-1",
            position_lot_id="lot_exit_state_1",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "CALL", "initial_qty": 2.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot_exit_state_1", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot_exit_state_1", filled_qty=2.0)

    await strat.kernel._submit_plugin_exit_intent(
        ctx={"now": opened_at.replace(minute=9)},
        exit_plan=ExitPlan(
            ts=opened_at.replace(minute=9),
            position_lot_id="lot_exit_state_1",
            qty=2.0,
            reason="PURE_EXIT",
        ),
    )

    state_rows = [row for row in writer.rows if str(row.get("domain")) == "state"]
    flatten_pairs = [(str(row.get("stage")), str(row.get("status")), str(row.get("reason"))) for row in state_rows if str(row.get("stage")) == "flatten_transition"]
    assert ("flatten_transition", "submitted", "exit_submitted") in flatten_pairs
    assert ("flatten_transition", "confirmed", "position_reconciled_flat") in flatten_pairs


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_mode_can_submit_exit_for_owned_lot():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureRoundTripStrategy(ctx, params)
    await strat.initialize()

    await strat.on_features("QQQ", {"close": 100.0, "ts": datetime(2026, 3, 25, 10, 4, 0)})
    ctx.portfolio.return_value = {"positions": [{"symbol": "OPT:QQQ:20260326:600:C", "qty": 2.0, "avg_px": 1.05}]}
    await strat.on_features("QQQ", {"close": 99.0, "ts": datetime(2026, 3, 25, 10, 5, 0)})

    assert ctx.submit_intent.await_count == 2
    entry_env = ctx.submit_intent.await_args_list[0].args[0]
    exit_env = ctx.submit_intent.await_args_list[1].args[0]
    assert float(entry_env.target_qty) == 2.0
    assert float(exit_env.target_qty) == -2.0
    assert str((exit_env.extras or {}).get("position_id")) == str((entry_env.extras or {}).get("position_id"))
    assert str((exit_env.extras or {}).get("exit_reason")) == "PURE_EXIT"
    snapshot = strat.kernel.state_snapshot()
    assert snapshot["ledger"]["lots"][0]["status"] == "pending_exit"


@pytest.mark.asyncio
async def test_option_kernel_pure_plugin_mode_can_submit_multiple_exit_plans():
    ctx = _make_ctx()
    params = StrategyParams(extras={"kernel_mode": "pure_plugin"})
    strat = _PureMultiExitStrategy(ctx, params)
    await strat.initialize()

    now = datetime(2026, 3, 25, 10, 6, 0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d1",
            symbol="OPT:QQQ:20260326:585:P",
            target_qty=1.0,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            order_ref="oref1",
            position_lot_id="lot1",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "PUT", "initial_qty": 1.0, "entry_mid": 1.2},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot1", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot1", filled_qty=1.0)
    strat.kernel.ledger.record_intent(
        KernelIntentRecord(
            strategy_id=strat.strategy_id,
            decision_id="d2",
            symbol="OPT:QQQ:20260326:580:P",
            target_qty=2.0,
            ts_bar_end_us=int(now.timestamp() * 1_000_000),
            order_ref="oref2",
            position_lot_id="lot2",
            decision_snapshot_id="",
            execution_profile_id="prof",
            payload={"side": "PUT", "initial_qty": 2.0, "entry_mid": 1.4},
        )
    )
    strat.kernel.ledger.resolve_entry_intent(position_lot_id="lot2", accepted=True)
    strat.kernel.ledger.mark_entry_live(position_lot_id="lot2", filled_qty=2.0)

    ctx.portfolio.return_value = {
        "positions": [
            {"symbol": "OPT:QQQ:20260326:585:P", "qty": 1.0, "avg_px": 1.2},
            {"symbol": "OPT:QQQ:20260326:580:P", "qty": 2.0, "avg_px": 1.4},
        ]
    }
    await strat.on_features("QQQ", {"close": 100.0, "ts": now})

    assert ctx.submit_intent.await_count == 2
    exits = [call.args[0] for call in ctx.submit_intent.await_args_list]
    assert {float(env.target_qty) for env in exits} == {-1.0, -2.0}
    assert {str((env.extras or {}).get("exit_reason")) for env in exits} == {"EXIT_A", "EXIT_B"}


@pytest.mark.asyncio
async def test_qqq_0dte_pure_strategy_can_round_trip_with_stub_quotes_and_marks(monkeypatch):
    ctx = _make_ctx()
    ctx.strategy_id = "qqq_0dte_pure_roundtrip"
    strat = QQQ0DTEOptionsLivePureStrategy(
        ctx,
        StrategyParams(
            extras={
                "kernel_mode": "pure_plugin",
                "mark_quote_max_age_sec": 999,
                "early_override_enabled": False,
                "path_b_enabled": False,
                "rvol_min": 0.0,
                "atr_expansion_mult": 0.1,
            }
        ),
    )
    await strat.initialize()

    def fake_select(now, *, side, spot, spread_max_pct):
        return (
            {
                "opt_symbol": "OPT:QQQ:20260326:500:C" if side == "CALL" else "OPT:QQQ:20260326:500:P",
                "expiry": "20260326",
                "strike": 500.0,
                "bid": 1.0,
                "ask": 1.1,
                "mid": 1.05,
                "last": 1.05,
                "quote_age_sec": 0.0,
            },
            None,
        )

    mark_values = iter([1.30, 1.30])

    monkeypatch.setattr(strat.plugin, "_select_0dte_quote_pure", fake_select)
    monkeypatch.setattr(strat.plugin, "_get_mark_for_contract_pure", lambda now, opt_symbol: next(mark_values, 1.30))
    monkeypatch.setattr(strat.plugin, "_enter_with_gates", lambda now, bar, direction, breakout_level: True)

    base = datetime(2026, 3, 25, 9, 31, 0)
    closes = [100.00, 100.03, 100.06, 100.08, 100.10, 100.12, 100.30, 100.46, 100.50]
    highs = [100.05, 100.07, 100.10, 100.12, 100.14, 100.16, 100.34, 100.52, 100.56]
    lows = [99.98, 100.00, 100.02, 100.05, 100.07, 100.10, 100.11, 100.24, 100.40]
    volumes = [1000, 1050, 1100, 1150, 1200, 1250, 2600, 3000, 2800]
    vwaps = [99.99, 100.01, 100.04, 100.06, 100.08, 100.10, 100.18, 100.32, 100.40]

    for i, close in enumerate(closes):
        if i == 7:
            ctx.portfolio.return_value = {
                "positions": [
                    {
                        "symbol": "OPT:QQQ:20260326:500:C",
                        "qty": 3.0,
                        "avg_px": 1.05,
                    }
                ]
            }
        await strat.on_features(
            "QQQ",
            {
                "ts": base.replace(minute=31 + i),
                "open": close - 0.02,
                "high": highs[i],
                "low": lows[i],
                "close": close,
                "vwap_1m": vwaps[i],
                "volume": volumes[i],
                "atr_d": 2.0,
            },
        )

    assert ctx.submit_intent.await_count >= 2
    entry_env = ctx.submit_intent.await_args_list[0].args[0]
    exit_env = ctx.submit_intent.await_args_list[1].args[0]
    assert float(entry_env.target_qty) == 3.0
    assert str(entry_env.symbol) == "OPT:QQQ:20260326:500:C"
    assert float(exit_env.target_qty) < 0.0


def test_qqq_1dte_pure_exit_plan_hits_tp1():
    plugin = QQQ1DTEMa10RejectPlugin()
    plugin.apply_default_extras({"contracts": 4})
    bars = [
        {"ts": datetime(2026, 3, 25, 10, 0), "open": 100.0, "high": 100.4, "low": 99.8, "close": 100.0, "ma10": 99.9},
        {"ts": datetime(2026, 3, 25, 10, 1), "open": 100.2, "high": 101.0, "low": 100.1, "close": 100.8, "ma10": 100.1},
        {"ts": datetime(2026, 3, 25, 10, 2), "open": 101.0, "high": 102.4, "low": 100.9, "close": 102.1, "ma10": 100.8},
    ]
    pos = KernelPositionState(
        strategy_id="qqq_1dte_pure_test",
        position_lot_id="lot1",
        opt_symbol="OPT:QQQ:20260326:600:C",
        side="CALL",
        qty=4.0,
        remaining_qty=4.0,
        opened_at=datetime(2026, 3, 25, 10, 0),
        avg_entry_px=1.05,
        structure_stop=99.0,
        target_1r=102.0,
        target_1p2r=102.4,
        status="intent_open",
        payload={"entry_underlying": 100.0, "initial_qty": 4.0},
    )
    runtime = _make_runtime_for_exit(now=bars[-1]["ts"], history=bars, pos=pos)
    plan = plugin.build_exit_plan_pure(runtime)
    assert plan is not None
    assert plan.reason == "TP1_1R"
    assert float(plan.qty) == 2.0


def test_qqq_1dte_pure_exit_plan_runner_ma10_fail_after_partial():
    plugin = QQQ1DTEMa10RejectPlugin()
    plugin.apply_default_extras({"contracts": 4})
    bars = [
        {"ts": datetime(2026, 3, 25, 10, 0), "open": 100.0, "high": 100.3, "low": 99.8, "close": 100.0, "ma10": 99.9},
        {"ts": datetime(2026, 3, 25, 10, 1), "open": 100.9, "high": 101.5, "low": 100.7, "close": 101.3, "ma10": 100.2},
        {"ts": datetime(2026, 3, 25, 10, 2), "open": 101.4, "high": 101.6, "low": 100.4, "close": 100.5, "ma10": 100.9},
    ]
    pos = KernelPositionState(
        strategy_id="qqq_1dte_pure_test",
        position_lot_id="lot2",
        opt_symbol="OPT:QQQ:20260326:600:C",
        side="CALL",
        qty=4.0,
        remaining_qty=2.0,
        opened_at=datetime(2026, 3, 25, 10, 0),
        avg_entry_px=1.05,
        structure_stop=99.0,
        target_1r=102.0,
        target_1p2r=102.4,
        status="partial_exit",
        payload={"entry_underlying": 100.0, "initial_qty": 4.0},
    )
    runtime = _make_runtime_for_exit(now=bars[-1]["ts"], history=bars, pos=pos)
    plan = plugin.build_exit_plan_pure(runtime)
    assert plan is not None
    assert plan.reason == "RUNNER_MA10_FAIL"
    assert float(plan.qty) == 2.0


def test_qqq_1dte_pure_exit_plan_soft_time_stop():
    plugin = QQQ1DTEMa10RejectPlugin()
    plugin.apply_default_extras({"contracts": 4})
    bars = []
    for i, close in enumerate([100.0, 100.1, 100.15, 100.12, 100.08, 100.05]):
        bars.append(
            {
                "ts": datetime(2026, 3, 25, 10, i),
                "open": close,
                "high": close + 0.08,
                "low": close - 0.05,
                "close": close,
                "ma10": 99.9,
            }
        )
    pos = KernelPositionState(
        strategy_id="qqq_1dte_pure_test",
        position_lot_id="lot3",
        opt_symbol="OPT:QQQ:20260326:600:C",
        side="CALL",
        qty=4.0,
        remaining_qty=4.0,
        opened_at=datetime(2026, 3, 25, 10, 0),
        avg_entry_px=1.05,
        structure_stop=99.0,
        target_1r=102.0,
        target_1p2r=102.4,
        status="intent_open",
        payload={"entry_underlying": 100.0, "initial_qty": 4.0},
    )
    runtime = _make_runtime_for_exit(now=bars[-1]["ts"], history=bars, pos=pos)
    plan = plugin.build_exit_plan_pure(runtime)
    assert plan is not None
    assert plan.reason == "SOFT_TIME_STOP"
    assert float(plan.qty) == 4.0
