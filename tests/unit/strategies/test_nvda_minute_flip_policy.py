from __future__ import annotations

import pytest

from strategies.stocks_kernel.policies.nvda_minute_flip import build_nvda_minute_flip_policy
from strategies.stocks_kernel.plugins.nvda_minute_flip import NVDAMinuteFlipStockPlugin
from strategies.stocks_kernel.runtime.submit_bridge import StrategySubmitBridge
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateEvaluationReport, HistoryBundle, MarketSnapshot


def _ctx(ts_epoch_s: float) -> FeatureContext:
    return FeatureContext(
        symbol="NVDA",
        snapshot=MarketSnapshot(symbol="NVDA", ts_epoch_s=ts_epoch_s, last_price=100.0, session="OVERNIGHT"),
        history=HistoryBundle(symbol="NVDA"),
        values={},
        readiness={},
    )


def _gate(allowed: bool = True) -> GateEvaluationReport:
    return GateEvaluationReport(allowed=allowed, decisions=(), blocked_by=None)


def test_nvda_minute_flip_policy_emits_one_entry_per_entry_bucket() -> None:
    policy = build_nvda_minute_flip_policy({"symbol": "NVDA", "qty": 1, "trade_interval_sec": 60}, {})
    ctx = _ctx(120.0)
    signal = policy.build_signal(symbol="NVDA", feature_context=ctx, gate_report=_gate())

    actions = policy.build_action_plans(signal=signal, feature_context=ctx)
    duplicate = policy.build_action_plans(signal=signal, feature_context=ctx)

    assert len(actions) == 1
    assert actions[0].action_type == "ENTRY"
    assert actions[0].symbol == "NVDA"
    assert actions[0].side == "LONG"
    assert actions[0].qty == 1
    assert actions[0].trigger_id == "nvda_minute_flip:2:enter"
    assert duplicate == []


def test_nvda_minute_flip_policy_emits_flatten_on_alternate_bucket() -> None:
    policy = build_nvda_minute_flip_policy({"symbol": "NVDA", "qty": 1, "trade_interval_sec": 60}, {})
    ctx = _ctx(180.0)
    signal = policy.build_signal(symbol="NVDA", feature_context=ctx, gate_report=_gate())
    actions = policy.build_action_plans(signal=signal, feature_context=ctx)

    assert len(actions) == 1
    assert actions[0].action_type == "FLATTEN"
    assert actions[0].symbol == "NVDA"
    assert actions[0].close_only is True
    assert actions[0].flatten_action is True
    assert actions[0].trigger_id == "nvda_minute_flip:3:flatten"
    assert actions[0].payload["force_order_style"] is True
    assert actions[0].payload["exit_order_type"] == "LMT"
    assert actions[0].payload["exit_limit_offset_bp"] == 1000.0
    assert actions[0].payload["suppress_manual_intervention_lock"] is True


def test_nvda_minute_flip_policy_holds_when_gate_blocked() -> None:
    policy = build_nvda_minute_flip_policy({"symbol": "NVDA"}, {})
    signal = policy.build_signal(symbol="NVDA", feature_context=_ctx(120.0), gate_report=_gate(False))

    assert signal.action == "hold"
    assert policy.build_action_plans(signal=signal, feature_context=_ctx(120.0)) == []


@pytest.mark.asyncio
async def test_nvda_minute_flip_plugin_marks_limit_order_style() -> None:
    class Envelope:
        def __init__(self) -> None:
            self.extras = {}
            self.order_style = None

    plugin = NVDAMinuteFlipStockPlugin(limit_offset_bp=20, order_type="LMT")
    await plugin.on_features(strategy=None, symbol="NVDA", features={"last_price": 100.0})
    envelope = Envelope()

    updated = await plugin.on_intent(strategy=None, envelope=envelope, symbol="NVDA", side="LONG")

    assert updated is envelope
    assert envelope.extras["stock_plugin_component"] == "nvda_minute_flip_stock_plugin"
    assert envelope.extras["test_strategy"] is True
    assert envelope.extras["paper_test"] is True
    assert envelope.order_style.kind == "LMT"
    assert envelope.order_style.limit_px == 100.2


def test_submit_bridge_builds_aggressive_flatten_limit_from_payload() -> None:
    class Lot:
        side = "LONG"
        position_lot_id = "lot-nvda-1"
        lane_id = "nvda_long_exec"
        ownership_scope = "strategy"

    class ExitPlan:
        symbol = "NVDA"
        order_role = "FLATTEN"
        qty = 1.0
        payload = {
            "force_order_style": True,
            "exit_order_type": "LMT",
            "exit_limit_offset_bp": 75.0,
        }

    bridge = StrategySubmitBridge(
        strategy_id="nvda_minute_flip_paper_001",
        strategy_run_id="run-1",
        ttl_ms=30000,
        dry_run=False,
    )

    envelope = bridge.build_exit_envelope(exit_plan=ExitPlan(), snapshot=_ctx(120.0).snapshot, lot=Lot())

    assert envelope.order_style.kind == "LMT"
    assert envelope.order_style.limit_px == 99.25
    assert envelope.extras["force_order_style"] is True
