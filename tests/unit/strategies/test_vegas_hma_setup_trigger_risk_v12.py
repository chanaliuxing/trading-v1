from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from libs.strategy.components.indicator_lib import IndicatorLib
from strategies.options_kernel.types import EntryPlan, KernelBarContext, KernelPositionState, PluginRuntimeState, Signal
from strategies.options_plugins.vegas_hma_setup_trigger_risk_v12 import (
    VegasBar,
    VegasHMAConfig,
    VegasHMASetupTriggerRiskV12Core,
    VegasHMASetupTriggerRiskV12Plugin,
    VegasState,
)


ET = ZoneInfo("America/New_York")


def _ts(i: int) -> datetime:
    return datetime(2026, 5, 6, 9, 35, tzinfo=ET) + timedelta(minutes=i)


def _long_sequence() -> list[tuple[float, float, float, float]]:
    seq: list[tuple[float, float, float, float]] = []
    price = 100.0
    for _ in range(180):
        seq.append((price, price + 0.08, price - 0.08, price + 0.03))
        price += 0.03
    for idx in range(10):
        if idx < 4:
            seq.append((price, price + 0.02, price - 0.20, price - 0.08))
            price -= 0.02
        else:
            seq.append((price, price + 0.35, price - 0.02, price + 0.28))
            price += 0.08
    return seq


def _short_sequence() -> list[tuple[float, float, float, float]]:
    seq: list[tuple[float, float, float, float]] = []
    price = 120.0
    for _ in range(180):
        seq.append((price, price + 0.08, price - 0.08, price - 0.03))
        price -= 0.03
    for idx in range(10):
        if idx < 4:
            seq.append((price, price + 0.20, price - 0.02, price + 0.08))
            price += 0.02
        else:
            seq.append((price, price + 0.02, price - 0.35, price - 0.28))
            price -= 0.08
    return seq


def _run_sequence(seq: list[tuple[float, float, float, float]], cfg: VegasHMAConfig | None = None):
    core = VegasHMASetupTriggerRiskV12Core(cfg or VegasHMAConfig(quality_len=300))
    decisions = []
    for i, row in enumerate(seq):
        decisions.append(core.on_bar(VegasBar(_ts(i), *row, 1000)))
    return core, decisions


def test_hma_helper_returns_warmed_value_and_none_before_warmup():
    assert IndicatorLib.hma([1.0, 2.0, 3.0], 12) is None
    value = IndicatorLib.hma([float(i) for i in range(1, 40)], 12)
    assert value is not None
    assert value > 30.0


def test_v11_atr_matches_pine_wilder_rma_seed():
    core = VegasHMASetupTriggerRiskV12Core(VegasHMAConfig(atr_len=3))
    bars = [
        VegasBar(_ts(0), 10.0, 11.0, 9.0, 10.0),
        VegasBar(_ts(1), 10.0, 12.0, 9.5, 11.0),
        VegasBar(_ts(2), 11.0, 13.0, 10.0, 12.0),
        VegasBar(_ts(3), 12.0, 13.5, 11.5, 13.0),
    ]

    atr = core._atr_series(bars, 3)

    assert atr[0] is None
    assert atr[1] is None
    assert atr[2] == pytest.approx((2.0 + 2.5 + 3.0) / 3.0)
    assert atr[3] == pytest.approx((((2.0 + 2.5 + 3.0) / 3.0) * 2.0 + 2.0) / 3.0)


def test_v11_session_matches_ny_weekday_window():
    core = VegasHMASetupTriggerRiskV12Core()

    assert core._in_session(datetime(2026, 5, 6, 9, 35, tzinfo=ET)) is True
    assert core._in_session(datetime(2026, 5, 6, 15, 45, tzinfo=ET)) is True
    assert core._in_session(datetime(2026, 5, 6, 9, 34, tzinfo=ET)) is False
    assert core._in_session(datetime(2026, 5, 9, 10, 0, tzinfo=ET)) is False


def test_v11_long_trigger_matches_pullback_resume_breakout():
    core, decisions = _run_sequence(_long_sequence())
    entries = [d for d in decisions if d.action == "ENTRY"]

    assert len(entries) == 1
    decision = entries[0]
    assert decision.signal == "BUY_CALL"
    assert decision.metadata["long_trigger"] is True
    assert decision.metadata["long_breakout"] is True
    assert decision.metadata["long_pullback_seen"] is True
    assert decision.metadata["long_resume_candle"] is True
    assert decision.metadata["long_quality_ok"] is True
    assert decision.metadata["not_too_extended_long"] is True
    assert core.state.position_side == "LONG_CALL_SIGNAL"


def test_v11_short_trigger_matches_pullback_resume_breakout():
    core, decisions = _run_sequence(_short_sequence())
    entries = [d for d in decisions if d.action == "ENTRY"]

    assert len(entries) == 1
    decision = entries[0]
    assert decision.signal == "BUY_PUT"
    assert decision.metadata["short_trigger"] is True
    assert decision.metadata["short_breakout"] is True
    assert decision.metadata["short_pullback_seen"] is True
    assert decision.metadata["short_resume_candle"] is True
    assert decision.metadata["short_quality_ok"] is True
    assert decision.metadata["not_too_extended_short"] is True
    assert core.state.position_side == "SHORT_PUT_SIGNAL"


def test_v11_rejects_long_when_distance_from_fast_hma_is_too_large():
    cfg = VegasHMAConfig(quality_len=300, max_dist_from_fhma_atr=0.01)
    _, decisions = _run_sequence(_long_sequence(), cfg)

    assert not any(d.action == "ENTRY" for d in decisions)
    assert any(d.metadata.get("long_breakout") and not d.metadata.get("not_too_extended_long") for d in decisions)


def test_v11_rejects_short_when_distance_from_fast_hma_is_too_large():
    cfg = VegasHMAConfig(quality_len=300, max_dist_from_fhma_atr=0.01)
    _, decisions = _run_sequence(_short_sequence(), cfg)

    assert not any(d.action == "ENTRY" for d in decisions)
    assert any(d.metadata.get("short_breakout") and not d.metadata.get("not_too_extended_short") for d in decisions)


def test_v11_body_filter_blocks_small_body_trigger():
    cfg = VegasHMAConfig(quality_len=300, chop_body_mult=2.0)
    _, decisions = _run_sequence(_long_sequence(), cfg)

    assert not any(d.action == "ENTRY" for d in decisions)
    assert any(d.metadata.get("long_breakout") and not d.metadata.get("is_valid_body") for d in decisions)


def test_v11_core_daily_max_does_not_change_pine_signals():
    cfg = VegasHMAConfig(quality_len=300, daily_max_trades=0)
    _, decisions = _run_sequence(_long_sequence(), cfg)

    assert any(d.action == "ENTRY" and d.signal == "BUY_CALL" for d in decisions)


def test_v11_long_atr_stop_only_moves_up_and_exits_on_close_cross():
    core, decisions = _run_sequence(_long_sequence())
    entry = next(d for d in decisions if d.action == "ENTRY")
    first_stop = float(entry.metadata["dynamic_stop_reference"])
    hold = core.on_bar(VegasBar(_ts(190), 106.0, 106.4, 105.9, 106.3, 1000))
    second_stop = float(hold.metadata["dynamic_stop_reference"])
    exit_bar = core.on_bar(VegasBar(_ts(191), second_stop + 0.02, second_stop + 0.03, second_stop - 0.05, second_stop - 0.01, 1000))

    assert second_stop >= first_stop
    assert exit_bar.action == "EXIT"
    assert exit_bar.signal == "EXIT_CALL"
    assert exit_bar.metadata["exit_reason"] == "V11_ATR_TRAILING_STOP"


def test_v11_short_atr_stop_only_moves_down_and_exits_on_close_cross():
    core, decisions = _run_sequence(_short_sequence())
    entry = next(d for d in decisions if d.action == "ENTRY")
    first_stop = float(entry.metadata["dynamic_stop_reference"])
    hold = core.on_bar(VegasBar(_ts(190), 114.0, 114.1, 113.6, 113.7, 1000))
    second_stop = float(hold.metadata["dynamic_stop_reference"])
    exit_bar = core.on_bar(VegasBar(_ts(191), second_stop - 0.02, second_stop + 0.05, second_stop - 0.03, second_stop + 0.01, 1000))

    assert second_stop <= first_stop
    assert exit_bar.action == "EXIT"
    assert exit_bar.signal == "EXIT_PUT"
    assert exit_bar.metadata["exit_reason"] == "V11_ATR_TRAILING_STOP"


def test_v11_duplicate_entry_guard_on_same_timestamp():
    core, decisions = _run_sequence(_long_sequence())
    entry_idx = next(i for i, d in enumerate(decisions) if d.action == "ENTRY")
    row = _long_sequence()[entry_idx]
    core.state.position_side = None
    duplicate = core.on_bar(VegasBar(_ts(entry_idx), *row, 1000))

    assert duplicate.action == "NONE"
    assert duplicate.metadata["block_reason"] == "DUPLICATE_BAR"


def test_ibkr_trailing_stop_order_spec_supports_amount_mode():
    cfg = VegasHMAConfig(quality_len=300, trailing_mode="amount", trailing_amount=0.25)
    _, decisions = _run_sequence(_long_sequence(), cfg)
    decision = next(d for d in decisions if d.action == "ENTRY")

    spec = decision.exit_plan.ibkr_trailing_order_spec
    assert spec.orderType == "TRAIL"
    assert spec.action == "SELL"
    assert spec.trailingPercent is None
    assert spec.auxPrice == pytest.approx(0.25)
    assert decision.exit_plan.conversion_policy == "UNDERLYING_TRIGGERED_OPTION_EXIT"


def test_live_plugin_rolls_back_optimistic_position_when_entry_blocked():
    plugin = VegasHMASetupTriggerRiskV12Plugin()
    plugin._pending_entry_pre_state = VegasState(trades_today=0)
    plugin.core.state.position_side = "LONG_CALL_SIGNAL"
    plugin.core.state.entry_reference_price = 100.0
    plugin.core.state.trades_today = 1

    plugin.on_entry_submit_blocked_pure(
        runtime=_runtime(),
        signal=Signal(ts=_ts(1), side="CALL", signal_kind="BUY_CALL"),
        entry_plan=EntryPlan(ts=_ts(1), side="CALL", signal_kind="x", structure_stop=99.0, target_1r=101.0, target_1p2r=101.2, r_per_share=1.0, contracts=1),
        selection=None,
        reason="no_contract_selection",
        payload={},
    )

    assert plugin.core.state.position_side is None
    assert plugin.core.state.entry_reference_price is None
    assert plugin.core.state.trades_today == 0
    assert plugin.core.state.last_signal_ts == _ts(1)


def test_live_plugin_does_not_emit_exit_plan_without_active_position():
    plugin = VegasHMASetupTriggerRiskV12Plugin()
    plugin._last_decision = type("Decision", (), {"action": "EXIT", "metadata": {"exit_reason": "V11_ATR_TRAILING_STOP"}})()

    assert plugin.build_exit_plans_pure(_runtime(active_position=None)) == []


def test_live_plugin_exit_plan_carries_underlying_trigger_and_ibkr_trail_payload():
    plugin = VegasHMASetupTriggerRiskV12Plugin(VegasHMAConfig(trailing_percent=9.5))
    plugin._last_decision = type(
        "Decision",
        (),
        {
            "action": "EXIT",
            "side": "CALL",
            "signal": "EXIT_CALL",
            "metadata": {"exit_reason": "V11_ATR_TRAILING_STOP", "dynamic_stop_reference": 501.25},
        },
    )()
    pos = KernelPositionState(
        strategy_id="vegas_v12_test",
        position_lot_id="lot-1",
        opt_symbol="OPT:SPY:20260508:500:C",
        side="CALL",
        qty=1,
        remaining_qty=1,
        opened_at=_ts(1),
        status="live",
    )

    plans = plugin.build_exit_plans_pure(_runtime(active_position=pos))

    assert len(plans) == 1
    payload = plans[0].payload
    assert payload["exit_model"] == "UNDERLYING_TRIGGERED_OPTION_EXIT"
    assert payload["underlying_trigger"]["trigger_price"] == pytest.approx(501.25)
    assert payload["underlying_trigger"]["trigger_direction"] == "BELOW"
    assert payload["option_contract_id"] == "OPT:SPY:20260508:500:C"
    assert payload["ibkr_trailing_order_spec"]["orderType"] == "TRAIL"
    assert payload["ibkr_trailing_order_spec"]["trailingPercent"] == pytest.approx(9.5)


@pytest.mark.asyncio
async def test_live_plugin_enables_sfs_first_by_default_without_clickhouse_poll_subscription():
    class Ctx:
        strategy_id = "vegas_v12_test"

        class Log:
            def warning(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

        log = Log()
        subscriptions = []
        rfe_calls = []

        async def history(self, symbol, resolution, limit):
            return []

        async def subscribe_features(self, symbols, bar_sec=5):
            self.subscriptions.append((symbols, bar_sec))

        async def enable_realtime_feature_engine(self, symbols, spec=None, emit_interval_sec=1, warmup_bars=1):
            self.rfe_calls.append(
                {
                    "symbols": symbols,
                    "spec": dict(spec or {}),
                    "emit_interval_sec": emit_interval_sec,
                    "warmup_bars": warmup_bars,
                }
            )

    ctx = Ctx()
    params = type("Params", (), {"extras": {}})()
    plugin = VegasHMASetupTriggerRiskV12Plugin()

    await plugin.initialize_pure(ctx, params, type("Kernel", (), {"preload_bar_history": lambda *args, **kwargs: 0})())

    assert ctx.subscriptions == []
    assert ctx.rfe_calls == [
        {
            "symbols": ["SPY"],
            "spec": {
                "enabled": True,
                "emit_on": "1m",
                "timeframes": ["1s", "1m"],
                "lookbacks": {"1s": 3600, "1m": 600},
                "sink": {"enabled": False},
            },
            "emit_interval_sec": 1,
            "warmup_bars": 1,
        }
    ]
    assert params.extras["market_data_source"] == "sfs"
    assert params.extras["sfs_first"] is True
    assert params.extras["source_bar_sec"] == 60
    assert params.extras["realtime_bar_sec"] == 60


@pytest.mark.asyncio
async def test_live_plugin_can_fallback_to_5s_clickhouse_poll_when_sfs_first_disabled():
    class Ctx:
        strategy_id = "vegas_v12_test"

        class Log:
            def warning(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

        log = Log()
        subscriptions = []
        rfe_calls = []

        async def history(self, symbol, resolution, limit):
            return []

        async def subscribe_features(self, symbols, bar_sec=5):
            self.subscriptions.append((symbols, bar_sec))

        async def enable_realtime_feature_engine(self, symbols, spec=None, emit_interval_sec=1, warmup_bars=1):
            self.rfe_calls.append((symbols, spec, emit_interval_sec, warmup_bars))

    ctx = Ctx()
    params = type("Params", (), {"extras": {"sfs_first": False, "source_bar_sec": 5, "realtime_bar_sec": 5}})()
    plugin = VegasHMASetupTriggerRiskV12Plugin()

    await plugin.initialize_pure(ctx, params, type("Kernel", (), {"preload_bar_history": lambda *args, **kwargs: 0})())

    assert ctx.rfe_calls == []
    assert ctx.subscriptions == [(["SPY"], 5)]


def test_live_plugin_aggregates_5s_source_into_completed_1m_bar():
    plugin = VegasHMASetupTriggerRiskV12Plugin()
    plugin.apply_default_extras({"sfs_first": False, "source_bar_sec": 5, "realtime_bar_sec": 5})

    first = _runtime_at(datetime(2026, 5, 6, 9, 35, 5, tzinfo=ET), open_=100.0, high=100.2, low=99.9, close=100.1, volume=10)
    second = _runtime_at(datetime(2026, 5, 6, 9, 35, 10, tzinfo=ET), open_=100.1, high=100.4, low=100.0, close=100.3, volume=20)
    rollover = _runtime_at(datetime(2026, 5, 6, 9, 36, 5, tzinfo=ET), open_=100.3, high=100.5, low=100.2, close=100.4, volume=30)

    assert plugin._complete_runtime_bar(first) is None
    assert plugin._complete_runtime_bar(second) is None
    completed = plugin._complete_runtime_bar(rollover)

    assert completed is not None
    assert completed["ts"] == datetime(2026, 5, 6, 9, 36, tzinfo=ET)
    assert completed["open"] == pytest.approx(100.0)
    assert completed["high"] == pytest.approx(100.4)
    assert completed["low"] == pytest.approx(99.9)
    assert completed["close"] == pytest.approx(100.3)
    assert completed["volume"] == pytest.approx(30)


def _runtime(active_position=None) -> PluginRuntimeState:
    now = _ts(1)
    return _runtime_at(now, active_position=active_position)


def _runtime_at(
    now: datetime,
    *,
    open_: float = 100.0,
    high: float = 100.2,
    low: float = 99.8,
    close: float = 100.0,
    volume: float = 1000.0,
    active_position=None,
) -> PluginRuntimeState:
    return PluginRuntimeState(
        strategy_id="vegas_v12_test",
        kernel_mode="pure_plugin",
        symbol="SPY",
        now=now,
        wall_now=now,
        spot=close,
        bar=KernelBarContext(ts=now, symbol="SPY", open=open_, high=high, low=low, close=close, volume=volume),
        features={"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        active_position=active_position,
        payload={"bar_history": [], "pending_entry_count": 0},
    )
