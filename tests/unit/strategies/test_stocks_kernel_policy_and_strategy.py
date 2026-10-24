import asyncio
import json
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock
import time

import pytest

from strategies.qqq_intraday_rules import ReplaySample, run_replay_samples
from services.health import SystemHealthController
from strategies.stocks_kernel.assembly import KernelAssembly, build_default_kernel_assembly
from strategies.stocks_kernel.policies.qqq_intraday_policy_v1 import QQQIntradayPolicyV1, SPYIntradayPolicyV1
from strategies.stocks_kernel.plugin import BaseStockPlugin, StockStrategyManifest
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import (
    ActionType,
    ActionPlan,
    DataContextBundle,
    DataReadinessReport,
    EntryPlan,
    ExitPlan,
    FeatureContext,
    GateEvaluationReport,
    HistoryBar,
    HistoryBundle,
    KernelFillReport,
    MarketSnapshot,
    PositionLot,
    SignalIntent,
    SymbolDataContext,
)
ET = ZoneInfo("America/New_York")


class _ManifestStockPlugin(BaseStockPlugin):
    component_name = "manifest_stock_plugin"

    def production_manifest(self) -> StockStrategyManifest:
        return StockStrategyManifest(
            strategy_key="manifest_test",
            symbols=("NVDA",),
            policy_id="qqq_intraday_policy_v1",
            recovery_policy_id="stocks_kernel_close_only",
        )


class _BrokerStub:
    def __init__(self):
        self.position_qty = AsyncMock(return_value=0.0)
        self.open_orders = AsyncMock(return_value=[])


class _PriceCacheStub:
    def __init__(self, snapshot=None, minute_stats=None):
        self._snapshot = snapshot
        self._minute_stats = minute_stats or SimpleNamespace(vwap=None)

    def price(self, symbol):
        return self._snapshot

    def minute_stats(self, symbol):
        return self._minute_stats


@pytest.mark.asyncio
async def test_strategy_uses_injected_kernel_assembly_provider():
    class _GateRegistry:
        def register_from_specs(self, specs):
            return None

        def build(self, gate_id, enabled=True, config=None):
            return SimpleNamespace(gate_id=gate_id, enabled=enabled, config=config or {})

    class _PolicyRegistry:
        def __init__(self):
            self.calls = []

        def register_from_specs(self, specs):
            return None

        def build(self, **kwargs):
            self.calls.append(kwargs)
            return QQQIntradayPolicyV1()

    class _RuntimeStageRegistry:
        def register_from_specs(self, specs):
            return None

        def stage_specs(self):
            return ()

    provider_calls = []
    injected_assembly = KernelAssembly(
        gate_registry=_GateRegistry(),
        policy_registry=_PolicyRegistry(),
        runtime_stage_registry=_RuntimeStageRegistry(),
    )

    def _provider(*, extras=None):
        provider_calls.append(dict(extras or {}))
        return injected_assembly

    ctx = SimpleNamespace(
        strategy_id="kernel_test_injected_assembly_001",
        strategy_run_id="run-1",
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(
            subscribe_features=AsyncMock(return_value=None),
            subscribe_tick_data=AsyncMock(return_value={"QQQ": True, "TQQQ": True, "SQQQ": True}),
            history_features=AsyncMock(return_value=[]),
            market_data_service=SimpleNamespace(ib_adapter=None),
        ),
        subscribe_features=AsyncMock(return_value=None),
        subscribe_tick_data=AsyncMock(return_value={"QQQ": True, "TQQQ": True, "SQQQ": True}),
        history=AsyncMock(return_value=[]),
        price_cache=_PriceCacheStub(),
        kernel_assembly_provider=_provider,
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_injected_assembly_001",
        ttl_ms=2_000,
        extras={
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": [],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )

    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert len(provider_calls) == 1
    for key, value in params.extras.items():
        assert provider_calls[0][key] == value
    assert strategy.gate_registry is injected_assembly.gate_registry
    assert strategy.policy_registry is injected_assembly.policy_registry
    assert strategy.runtime_stage_registry is injected_assembly.runtime_stage_registry


@pytest.mark.asyncio
async def test_strategy_legacy_kernel_assembly_fallback_still_initializes():
    ctx = SimpleNamespace(
        strategy_id="kernel_test_legacy_assembly_001",
        strategy_run_id="run-1",
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(
            subscribe_features=AsyncMock(return_value=None),
            subscribe_tick_data=AsyncMock(return_value={"QQQ": True, "TQQQ": True, "SQQQ": True}),
            history_features=AsyncMock(return_value=[]),
            market_data_service=SimpleNamespace(ib_adapter=None),
        ),
        subscribe_features=AsyncMock(return_value=None),
        subscribe_tick_data=AsyncMock(return_value={"QQQ": True, "TQQQ": True, "SQQQ": True}),
        history=AsyncMock(return_value=[]),
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_legacy_assembly_001",
        ttl_ms=2_000,
        extras={
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": [],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )

    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    expected = build_default_kernel_assembly(extras=dict(params.extras))
    assert type(strategy.gate_registry) is type(expected.gate_registry)
    assert type(strategy.policy_registry) is type(expected.policy_registry)
    assert type(strategy.runtime_stage_registry) is type(expected.runtime_stage_registry)


@pytest.mark.asyncio
async def test_strategy_strict_injected_kernel_assembly_requires_explicit_provider():
    ctx = SimpleNamespace(
        strategy_id="kernel_test_strict_injected_assembly_001",
        strategy_run_id="run-1",
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(
            subscribe_features=AsyncMock(return_value=None),
            subscribe_tick_data=AsyncMock(return_value={"QQQ": True, "TQQQ": True, "SQQQ": True}),
            history_features=AsyncMock(return_value=[]),
            market_data_service=SimpleNamespace(ib_adapter=None),
        ),
        subscribe_features=AsyncMock(return_value=None),
        subscribe_tick_data=AsyncMock(return_value={"QQQ": True, "TQQQ": True, "SQQQ": True}),
        history=AsyncMock(return_value=[]),
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_strict_injected_assembly_001",
        ttl_ms=2_000,
        extras={
            "dry_run": True,
            "require_injected_kernel_assembly": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": [],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )

    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    with pytest.raises(ValueError, match="require_injected_kernel_assembly=true"):
        await strategy.initialize()


def test_qqq_intraday_policy_v1_builds_long_entry_plan():
    policy = QQQIntradayPolicyV1(max_trades_per_day=4, hard_stop_atr_mult=0.75, logic_retreat_min_hold_sec=360)
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())

    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)
    entry = policy.build_entry_plan(signal=signal, feature_context=feature_context)

    assert signal.action == "enter"
    assert signal.side == "LONG"
    assert entry is not None and entry.qty == 3.0
    assert entry.payload["hard_stop_atr_mult"] == 0.75
    stop_plan = policy.build_stop_plan(signal=signal, entry_plan=entry, feature_context=feature_context)
    take_profit_plan = policy.build_take_profit_plan(signal=signal, entry_plan=entry, feature_context=feature_context)
    retreat_plan = policy.build_retreat_plan(signal=signal, feature_context=feature_context)
    fill_feedback = policy.on_fill_feedback(
        position_lot_id="lot-1",
        order_role="ENTRY",
        filled_qty=3.0,
        fill_price=500.0,
        feature_context=feature_context,
    )

    assert stop_plan is not None and stop_plan.order_role == "STOP"
    assert take_profit_plan is not None and take_profit_plan.order_role == "TAKE_PROFIT"
    assert retreat_plan is not None and retreat_plan.order_role == "RETREAT"
    assert fill_feedback is not None and fill_feedback["position_lot_id"] == "lot-1"


def test_default_qqq_policy_pack_honors_explicit_rule_engine_parity_param():
    from strategies.stocks_kernel.policies.default_pack import build_qqq_intraday_policy

    policy = build_qqq_intraday_policy(params={"rule_engine_parity": True}, extras={"dry_run": False})

    assert isinstance(policy, QQQIntradayPolicyV1)
    assert policy.rule_engine_parity is True


def test_default_spy_policy_pack_reuses_qqq_logic_with_spy_identity():
    from strategies.stocks_kernel.policies.default_pack import build_spy_intraday_policy

    policy = build_spy_intraday_policy(params={"rule_engine_parity": True}, extras={"dry_run": False})

    assert isinstance(policy, SPYIntradayPolicyV1)
    assert policy.rule_engine_parity is True
    assert policy.runtime_strategy_id == "spy_intraday_policy_v1"


def test_qqq_intraday_policy_v1_build_action_plans_defaults_to_entry_action():
    policy = QQQIntradayPolicyV1(max_trades_per_day=4, hard_stop_atr_mult=0.75, logic_retreat_min_hold_sec=360)
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)

    actions = policy.build_action_plans(signal=signal, feature_context=feature_context)

    assert len(actions) == 1
    assert actions[0].action_type == ActionType.ENTRY
    assert actions[0].symbol == "QQQ"
    assert actions[0].side == "LONG"
    assert actions[0].qty == pytest.approx(3.0)


def test_qqq_intraday_policy_v1_blocks_third_trade_without_positive_day_pnl():
    policy = QQQIntradayPolicyV1()
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)

    decision = policy.evaluate_entry_permission(
        signal=signal,
        feature_context=feature_context,
        runtime_state={"success_count": 2, "day_realized_pnl": 0.0},
    )

    assert decision.allowed is False
    assert decision.reason == "third_trade_requires_positive_day_pnl"


def test_qqq_intraday_policy_v1_blocks_when_max_trades_reached():
    policy = QQQIntradayPolicyV1(max_trades_per_day=3)
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)

    decision = policy.evaluate_entry_permission(
        signal=signal,
        feature_context=feature_context,
        runtime_state={"success_count": 3, "day_realized_pnl": 50.0},
    )

    assert decision.allowed is False
    assert decision.reason == "max_trades_per_day"


def test_qqq_intraday_policy_v1_blocks_when_pending_entry_exists():
    policy = QQQIntradayPolicyV1()
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)

    decision = policy.evaluate_entry_permission(
        signal=signal,
        feature_context=feature_context,
        runtime_state={"pending_entry_count": 1, "pending_entry_symbols": ["TQQQ"]},
    )

    assert decision.allowed is False
    assert decision.reason == "pending_entry_inflight"


def test_qqq_intraday_policy_v1_blocks_when_same_strategy_pending_orders_exist():
    policy = QQQIntradayPolicyV1()
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)

    decision = policy.evaluate_entry_permission(
        signal=signal,
        feature_context=feature_context,
        runtime_state={"same_strategy_pending_orders": 2, "pending_entry_count": 0},
    )

    assert decision.allowed is False
    assert decision.reason == "same_strategy_pending_orders"


def test_qqq_intraday_policy_v1_blocks_when_order_lifecycle_pending_exists():
    policy = QQQIntradayPolicyV1()
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)

    decision = policy.evaluate_entry_permission(
        signal=signal,
        feature_context=feature_context,
        runtime_state={"order_lifecycle_pending_count": 1, "order_lifecycle_pending_refs": ["oref-1"]},
    )

    assert decision.allowed is False
    assert decision.reason == "order_lifecycle_pending"


def test_policy_build_action_plans_translates_exit_plans():
    class _PolicyWithExit(QQQIntradayPolicyV1):
        def build_exit_plans(self, *, feature_context):
            return [
                SimpleNamespace(symbol="TQQQ", order_role="FLATTEN", qty=1.0, position_lot_id="lot-a", payload={"reason": "eod"}),
            ]

    policy = _PolicyWithExit(max_trades_per_day=4, hard_stop_atr_mult=0.75, logic_retreat_min_hold_sec=360)
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 3, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.3},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)
    actions = policy.build_action_plans(signal=signal, feature_context=feature_context)

    assert any(a.action_type == ActionType.FLATTEN for a in actions)


def test_qqq_policy_rule_engine_parity_matches_legacy_allowed_and_side():
    policy = QQQIntradayPolicyV1(rule_engine_parity=True, qqew_decoupled=True)
    samples = [
        ReplaySample(
            name="baseline_long_allowed",
            now=datetime(2026, 4, 3, 10, 5, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.1, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={"entry_model": "baseline", "qqew_decoupled": True},
        ),
        ReplaySample(
            name="v47_fast_drift_blocked_pre0945",
            now=datetime(2026, 4, 3, 9, 35, 0),
            spot=500.0,
            features={"v3m": 0.9, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={"entry_model": "v47_fast_drift", "qqew_decoupled": True},
        ),
        ReplaySample(
            name="v46_ab_opening_allowed",
            now=datetime(2026, 4, 3, 9, 35, 0),
            spot=500.0,
            features={"v3m": 1.0, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True, "volume": 2000.0},
            extras={"entry_model": "v46_ab", "qqew_decoupled": True, "opening_breakout_vol_min": 0.0},
        ),
        ReplaySample(
            name="v44_elite_blocked_overextended",
            now=datetime(2026, 4, 3, 10, 15, 0),
            spot=505.0,
            features={"v3m": 0.8, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            extras={"entry_model": "v44_elite", "qqew_decoupled": True},
        ),
    ]
    legacy_rows = {item.name: item for item in run_replay_samples(samples)}

    for sample in samples:
        signal = policy.build_signal(
            symbol="QQQ",
            feature_context=FeatureContext(
                symbol="QQQ",
                snapshot=MarketSnapshot(
                    symbol="QQQ",
                    ts_epoch_s=sample.now.timestamp(),
                    last_price=sample.spot,
                    session="RTH",
                    vwap=float(sample.features.get("vwap", sample.spot)),
                    minute_close=sample.spot,
                    minute_volume=float(sample.features.get("volume", 0.0)),
                    extras=dict(sample.extras or {}),
                ),
                history=HistoryBundle(symbol="QQQ"),
                values=dict(sample.features or {}),
                readiness={},
            ),
            gate_report=GateEvaluationReport(allowed=True, decisions=()),
        )
        allowed = signal.action == "enter" and signal.side in {"LONG", "SHORT"}
        stock_side = str(signal.side or "NONE") if allowed else "NONE"
        expected = legacy_rows[sample.name]
        assert allowed == expected.allowed, sample.name
        assert stock_side == expected.stock_side, sample.name


def test_qqq_policy_rule_engine_parity_converts_v3m_ratio_to_price_delta_when_configured():
    policy = QQQIntradayPolicyV1(rule_engine_parity=True, qqew_decoupled=True, v3m_units="ratio")
    signal = policy.build_signal(
        symbol="QQQ",
        feature_context=FeatureContext(
            symbol="QQQ",
            snapshot=MarketSnapshot(
                symbol="QQQ",
                ts_epoch_s=datetime(2026, 4, 8, 10, 5, 0).timestamp(),
                last_price=500.0,
                session="RTH",
                vwap=499.0,
                minute_close=500.0,
                minute_volume=1000.0,
                extras={"entry_model": "baseline", "qqew_decoupled": True},
            ),
            history=HistoryBundle(symbol="QQQ"),
            values={"v3m": 0.003, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0, "qqew_decoupled": True},
            readiness={},
        ),
        gate_report=GateEvaluationReport(allowed=True, decisions=()),
    )
    assert signal.action == "enter"
    assert signal.side == "LONG"
    assert signal.payload.get("v3m_units_mode") == "ratio"
    assert signal.payload.get("v3m_units_converted") is True


def test_spy_policy_rule_engine_parity_consumes_rsp_confirm_scope():
    policy = SPYIntradayPolicyV1(rule_engine_parity=True, qqew_decoupled=False, static_extras={"confirm_feature_prefix": "rsp"})
    ts_epoch_s = datetime(2026, 4, 8, 10, 5, 0).timestamp()
    bundle = DataContextBundle(
        signal=SymbolDataContext(
            scope="signal",
            symbol="SPY",
            snapshot=MarketSnapshot(symbol="SPY", ts_epoch_s=ts_epoch_s, last_price=500.0, vwap=499.0, session="RTH"),
            history=HistoryBundle(symbol="SPY"),
            readiness=DataReadinessReport(True, "ready", True, True, True, {}),
            feature_context=None,
        ),
        confirm={
            "RSP": SymbolDataContext(
                scope="confirm",
                symbol="RSP",
                snapshot=MarketSnapshot(symbol="RSP", ts_epoch_s=ts_epoch_s, last_price=200.4, vwap=200.0, session="RTH"),
                history=HistoryBundle(
                    symbol="RSP",
                    bars_1m=(
                        HistoryBar(ts_epoch_s=ts_epoch_s - 180.0, open=199.8, high=199.9, low=199.7, close=199.8, volume=1000.0),
                        HistoryBar(ts_epoch_s=ts_epoch_s, open=200.2, high=200.5, low=200.1, close=200.4, volume=1200.0),
                    ),
                ),
                readiness=DataReadinessReport(True, "ready", True, True, True, {}),
                feature_context=None,
            )
        },
    )
    signal = policy.build_signal(
        symbol="SPY",
        feature_context=FeatureContext(
            symbol="SPY",
            snapshot=MarketSnapshot(
                symbol="SPY",
                ts_epoch_s=ts_epoch_s,
                last_price=500.0,
                session="RTH",
                vwap=499.0,
                minute_close=500.0,
                minute_volume=1000.0,
                extras={"entry_model": "baseline", "confirm_symbol": "RSP", "confirm_feature_prefix": "rsp"},
            ),
            history=HistoryBundle(symbol="SPY"),
            values={
                "v3m": 0.9,
                "vwap": 499.0,
                "atr_eff": 1.0,
                "atr_seed": 1.0,
                "__data_context_bundle__": bundle,
            },
            readiness={},
        ),
        gate_report=GateEvaluationReport(allowed=True, decisions=()),
    )
    assert signal.action == "enter"
    assert signal.side == "LONG"


def test_qqq_policy_rule_engine_parity_passes_static_extras_into_rule_engine():
    captured = {}

    class _EngineStub:
        def evaluate(self, *, runtime, extras, recipe):
            captured["extras"] = dict(extras or {})
            return SimpleNamespace(
                translation=SimpleNamespace(
                    allowed=False,
                    reason="blocked_for_test",
                    stock_side="NONE",
                    payload={},
                )
            )

    policy = QQQIntradayPolicyV1(
        rule_engine_parity=True,
        static_extras={"entry_model": "v47_fast_drift", "enable_fast": True, "ab_time_end": "14:00:00"},
    )
    policy._rule_engine = _EngineStub()
    signal = policy.build_signal(
        symbol="QQQ",
        feature_context=FeatureContext(
            symbol="QQQ",
            snapshot=MarketSnapshot(
                symbol="QQQ",
                ts_epoch_s=datetime(2026, 4, 8, 10, 5, 0).timestamp(),
                last_price=500.0,
                session="RTH",
                vwap=499.0,
                minute_close=500.0,
                minute_volume=1000.0,
                extras={},
            ),
            history=HistoryBundle(symbol="QQQ"),
            values={"v3m": 1.0, "vwap": 499.0, "atr_eff": 1.0, "atr_seed": 1.0},
            readiness={},
        ),
        gate_report=GateEvaluationReport(allowed=True, decisions=()),
    )
    assert signal.action == "hold"
    assert captured["extras"]["entry_model"] == "v47_fast_drift"
    assert captured["extras"]["enable_fast"] is True
    assert captured["extras"]["ab_time_end"] == "14:00:00"


@pytest.mark.asyncio
async def test_composable_strategy_initializes_and_records_latest_plan():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"hard_stop_atr_mult": 0.75},
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=1, open=100, high=101, low=99, close=100, volume=10),
                HistoryBar(ts_epoch_s=2, open=100, high=102, low=99, close=101, volume=20),
                HistoryBar(ts_epoch_s=3, open=101, high=103, low=100, close=102, volume=30),
                HistoryBar(ts_epoch_s=4, open=102, high=104, low=101, close=103, volume=40),
                HistoryBar(ts_epoch_s=5, open=103, high=105, low=102, close=104, volume=50),
                HistoryBar(ts_epoch_s=6, open=104, high=106, low=103, close=105, volume=60),
                HistoryBar(ts_epoch_s=7, open=105, high=107, low=104, close=106, volume=70),
                HistoryBar(ts_epoch_s=8, open=106, high=108, low=105, close=107, volume=80),
                HistoryBar(ts_epoch_s=9, open=107, high=109, low=106, close=108, volume=90),
                HistoryBar(ts_epoch_s=10, open=108, high=110, low=107, close=109, volume=100),
                HistoryBar(ts_epoch_s=11, open=109, high=111, low=108, close=110, volume=110),
                HistoryBar(ts_epoch_s=12, open=110, high=112, low=109, close=111, volume=120),
                HistoryBar(ts_epoch_s=13, open=111, high=113, low=110, close=112, volume=130),
                HistoryBar(ts_epoch_s=14, open=112, high=114, low=111, close=113, volume=140),
                HistoryBar(ts_epoch_s=15, open=113, high=115, low=112, close=114, volume=150),
            ],
        },
    )

    assert "QQQ" in strategy.kernel_state
    assert strategy.kernel_state["QQQ"]["gate_report"].allowed is True
    assert strategy.kernel_state["QQQ"]["signal"].action == "enter"
    assert strategy.kernel_state["QQQ"]["entry_plan"].qty == 2.0


@pytest.mark.asyncio
async def test_composable_strategy_passes_policy_params_into_policy_static_extras():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_policy_static_extras_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_policy_static_extras_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {
                "entry_model": "v47_fast_drift",
                "rule_engine_parity": True,
                "enable_fast": True,
                "ab_time_end": "14:00:00",
            },
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert isinstance(strategy.policy, QQQIntradayPolicyV1)
    assert strategy.policy.entry_model == "v47_fast_drift"
    assert strategy.policy.static_extras.get("enable_fast") is True
    assert strategy.policy.static_extras.get("ab_time_end") == "14:00:00"


@pytest.mark.asyncio
async def test_composable_strategy_applies_gate_overrides_from_config():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_gate_override_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_gate_override_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "gate_overrides": {"direction_match": {"min_abs_value": 0.05}},
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy.kernel_state["QQQ"]["gate_report"].allowed is False
    assert strategy.kernel_state["QQQ"]["gate_report"].blocked_by == "direction_match"


@pytest.mark.asyncio
async def test_composable_strategy_injects_planned_qty_from_capital_limit():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_planned_qty_inject_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_planned_qty_inject_001",
        capital_limit_usd=10_000,
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"hard_stop_atr_mult": 0.75},
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy.kernel_state["QQQ"]["gate_report"].allowed is True
    assert strategy.kernel_state["QQQ"]["signal"].action == "enter"
    assert strategy.kernel_state["QQQ"]["entry_plan"].qty == 20.0


@pytest.mark.asyncio
async def test_composable_strategy_clamps_disabled_safety_runtime_stage():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_stage_override_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_stage_override_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"hard_stop_atr_mult": 0.75},
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "runtime_stage_overrides": {"safety_invariants": {"enabled": False}},
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._check_runtime_safety_invariants = AsyncMock()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._check_runtime_safety_invariants.assert_awaited()
    assert strategy.runtime_orchestrator is not None
    stage = strategy.runtime_orchestrator.build_plan().stage("safety_invariants")
    assert stage is not None
    assert stage.enabled is True
    assert stage.metadata.get("enabled_clamped") is True


@pytest.mark.asyncio
async def test_composable_strategy_executes_dynamic_runtime_stage_from_config():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_dynamic_stage_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_dynamic_stage_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "enable_runtime_stage_registry": True,
            "runtime_extra_stages": [
                {
                    "stage_id": "zz_dynamic_marker",
                    "order": 3,
                    "module": "strategies.stocks_kernel.runtime.default_stage_pack",
                    "attr": "mark_dynamic_stage",
                }
            ],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert int(getattr(strategy, "_dynamic_stage_hit_count", 0)) == 1


@pytest.mark.asyncio
async def test_composable_strategy_rejects_dynamic_gate_specs_without_flag():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_dynamic_gate_flag_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_dynamic_gate_flag_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["dynamic_session_window"],
            "gate_registry_specs": [
                {
                    "gate_id": "dynamic_session_window",
                    "module": "strategies.stocks_kernel.gates.session_window",
                    "attr": "SessionWindowGate",
                    "default_config": {"allowed_sessions": ["RTH"]},
                }
            ],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)

    with pytest.raises(ValueError, match="enable_dynamic_gate_registry=true"):
        await strategy.initialize()


@pytest.mark.asyncio
async def test_composable_strategy_rejects_runtime_extra_stages_without_flag():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_dynamic_stage_flag_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_dynamic_stage_flag_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "runtime_extra_stages": [
                {
                    "stage_id": "zz_dynamic_marker",
                    "order": 3,
                    "module": "strategies.stocks_kernel.runtime.default_stage_pack",
                    "attr": "mark_dynamic_stage",
                }
            ],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)

    with pytest.raises(ValueError, match="enable_runtime_stage_registry=true"):
        await strategy.initialize()


@pytest.mark.asyncio
async def test_composable_strategy_rejects_dynamic_policy_specs_without_flag():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_dynamic_policy_flag_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_dynamic_policy_flag_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1_alias",
            "policy_registry_specs": [
                {
                    "policy_id": "qqq_intraday_policy_v1_alias",
                    "mode": "factory",
                    "module": "strategies.stocks_kernel.policies.default_pack",
                    "attr": "build_qqq_intraday_policy",
                }
            ],
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)

    with pytest.raises(ValueError, match="enable_dynamic_policy_registry=true"):
        await strategy.initialize()


@pytest.mark.asyncio
async def test_composable_strategy_rejects_plugin_manifest_config_mismatch():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_manifest_mismatch_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_manifest_mismatch_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness"],
            "plugin_instance": _ManifestStockPlugin(),
            "signal_symbol": "MSFT",
            "universe": [{"signal_symbol": "MSFT", "long_exec_symbol": "MSFT", "short_exec_symbol": "MSFT"}],
            "data_contract": {"production_blocking": True, "symbols": ["MSFT"]},
            "production_manifest": {
                "production_blocking": True,
                "entry_contract": {"uses_decision_pipeline": True, "uom_only": True},
                "recovery_contract": {
                    "policy_id": "stocks_kernel_close_only",
                    "unknown_owner_action": "quarantine",
                    "fallback": "flatten",
                },
                "eod_contract": {"no_overnight": True, "platform_eod_required": True},
                "tradability_reporting": {"reports_strategy_tradability_gate": True},
            },
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)

    with pytest.raises(ValueError, match="stock_plugin_manifest_contract_invalid"):
        await strategy.initialize()


@pytest.mark.asyncio
async def test_composable_strategy_emits_dynamic_registry_startup_summary_audit():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_dynamic_registry_summary_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_dynamic_registry_summary_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1_alias",
            "enable_dynamic_gate_registry": True,
            "enable_dynamic_policy_registry": True,
            "enable_runtime_stage_registry": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["dynamic_session_window"],
            "gate_registry_specs": [
                {
                    "gate_id": "dynamic_session_window",
                    "module": "strategies.stocks_kernel.gates.session_window",
                    "attr": "SessionWindowGate",
                    "default_config": {"allowed_sessions": ["RTH"]},
                }
            ],
            "policy_registry_specs": [
                {
                    "policy_id": "qqq_intraday_policy_v1_alias",
                    "mode": "factory",
                    "module": "strategies.stocks_kernel.policies.default_pack",
                    "attr": "build_qqq_intraday_policy",
                }
            ],
            "runtime_extra_stages": [
                {
                    "stage_id": "zz_dynamic_marker",
                    "order": 3,
                    "module": "strategies.stocks_kernel.runtime.default_stage_pack",
                    "attr": "mark_dynamic_stage",
                }
            ],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    captured = []

    def _capture_audit(*, stage, symbol, reason, payload):
        captured.append({"stage": stage, "symbol": symbol, "reason": reason, "payload": dict(payload)})

    strategy._audit = _capture_audit
    await strategy.initialize()

    summary = next((item for item in captured if item["stage"] == "dynamic_registry_startup_summary"), None)
    assert summary is not None
    assert summary["payload"]["enable_dynamic_gate_registry"] is True
    assert summary["payload"]["enable_dynamic_policy_registry"] is True
    assert summary["payload"]["enable_runtime_stage_registry"] is True
    assert summary["payload"]["gate_registry_specs_count"] == 1
    assert summary["payload"]["policy_registry_specs_count"] == 1
    assert summary["payload"]["runtime_extra_stages_count"] == 1
    assert int(summary["payload"]["registered_gate_count"]) >= 1
    assert int(summary["payload"]["registered_policy_count"]) >= 1
    assert int(summary["payload"]["registered_stage_count"]) >= 1


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_when_multi_scope_execution_required_not_ready():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_multi_scope_gate_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_multi_scope_gate_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"hard_stop_atr_mult": 0.75},
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "multi_scope_gate": {"enabled": True, "execution_required": True},
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._submit_intent = AsyncMock()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()
    plans = tuple(strategy.kernel_state.get("QQQ", {}).get("action_plans") or ())
    assert all(str(getattr(p, "action_type", "")).upper() not in {"ENTRY", "ADD"} for p in plans)


@pytest.mark.asyncio
async def test_composable_strategy_populates_multi_scope_data_context_bundle():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_multi_scope_bundle_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_multi_scope_bundle_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"hard_stop_atr_mult": 0.75},
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{
                "signal_symbol": "QQQ",
                "long_exec_symbol": "TQQQ",
                "short_exec_symbol": "SQQQ",
                "confirm_symbol": "VXN",
                "volatility_symbol": "SQQQ",
            }],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    strategy._remember_latest_snapshot(
        symbol="TQQQ",
        features={"ts_epoch_s": 100.0, "last_price": 50.0, "session": "RTH", "vwap": 49.5},
    )
    strategy._remember_latest_snapshot(
        symbol="VXN",
        features={"ts_epoch_s": 100.0, "last_price": 18.0, "session": "RTH"},
    )
    strategy._remember_latest_snapshot(
        symbol="SQQQ",
        features={"ts_epoch_s": 100.0, "last_price": 12.0, "session": "RTH"},
    )

    async def _history_load(*, symbol: str, resolution: str, limit: int):
        _ = (resolution, limit)
        return HistoryBundle(
            symbol=symbol,
            bars_1m=tuple(
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ),
        )

    strategy.history_provider.load = AsyncMock(side_effect=_history_load)

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    bundle = strategy.kernel_state["QQQ"]["data_context_bundle"]

    assert bundle.execution["TQQQ"].snapshot is not None
    assert bundle.confirm["VXN"].snapshot is not None
    assert bundle.hedge["SQQQ"].snapshot is not None
    assert bundle.execution["TQQQ"].feature_context.scope == "execution"


@pytest.mark.asyncio
async def test_composable_strategy_applies_scoped_feature_overlay_into_feature_context():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_scoped_features_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_scoped_features_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "scoped_feature_requirements": {"execution": ["vwap", "v3m"]},
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    strategy._remember_latest_snapshot(
        symbol="TQQQ",
        features={"ts_epoch_s": 100.0, "last_price": 50.0, "session": "RTH", "vwap": 49.5},
    )

    async def _history_load(*, symbol: str, resolution: str, limit: int):
        _ = (resolution, limit)
        return HistoryBundle(
            symbol=symbol,
            bars_1m=tuple(
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ),
        )

    strategy.history_provider.load = AsyncMock(side_effect=_history_load)

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    fctx = strategy.kernel_state["QQQ"]["feature_context"]
    assert "execution.vwap" in fctx.values
    assert "execution.v3m" in fctx.values


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_on_portfolio_exposure_limit():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_portfolio_limit_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_portfolio_limit_001",
        extras={
            "portfolio_symbol_limit_usd": 100.0,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._submit_intent = AsyncMock()
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_on_qqq_third_trade_pnl_gate():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_qqq_trade_gate_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_qqq_trade_gate_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._submit_intent = AsyncMock()
    await strategy.initialize()
    strategy._completed_round_trips_by_day["1970-01-01"] = {"lot-1", "lot-2"}
    strategy._day_realized_pnl_by_day["1970-01-01"] = 0.0

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_when_pending_entry_inflight():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_pending_entry_gate_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_pending_entry_gate_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._submit_intent = AsyncMock()
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="inflight-entry-lot",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id="t",
            entry_order_ref="oref-inflight",
            live_qty=0.0,
            filled_qty=0.0,
            avg_entry_price=None,
            state="pending_entry",
            extras={},
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_when_same_strategy_pending_orders_inflight():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_pending_orders_gate_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_pending_orders_gate_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._submit_intent = AsyncMock()
    await strategy.initialize()
    strategy._same_strategy_pending_orders = lambda exec_symbols: __import__("asyncio").sleep(0, result=2)

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_when_order_lifecycle_pending_inflight():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_order_lifecycle_gate_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_order_lifecycle_gate_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._submit_intent = AsyncMock()
    await strategy.initialize()
    strategy._same_strategy_pending_orders = lambda exec_symbols: __import__("asyncio").sleep(0, result=0)
    strategy.order_lifecycle_module.register_created(
        order_ref="stkv1:test:TQQQ:ENTRY:LOT:1",
        symbol="TQQQ",
        position_lot_id="lot-1",
        now_ts_epoch_s=95.0,
    )

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_records_trade_day_stats_on_closed_exit_fill():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_trade_stats_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_trade_stats_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    prior_lot = PositionLot(
        position_lot_id="lot-closed-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d1",
        opened_by_trigger_id="t1",
        entry_order_ref="oref-1",
        live_qty=1.0,
        filled_qty=1.0,
        avg_entry_price=50.0,
        state="live",
        extras={},
    )
    current_lot = PositionLot(
        position_lot_id="lot-closed-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d1",
        opened_by_trigger_id="t1",
        entry_order_ref="oref-1",
        live_qty=0.0,
        filled_qty=1.0,
        avg_entry_price=50.0,
        state="closed",
        extras={},
    )

    strategy._record_fill_trade_stats(
        fill=KernelFillReport(symbol="TQQQ", order_ref="oref-exit-1", qty=1.0, price=52.0),
        apply_result=SimpleNamespace(order_role="FLATTEN"),
        prior_lot=prior_lot,
        current_lot=current_lot,
    )

    day_key = strategy._day_key(ts_epoch_s=float(datetime.now(ET).timestamp()))
    assert strategy._completed_round_trip_count(ts_epoch_s=float(datetime.now(ET).timestamp())) == 1
    assert strategy._day_realized_pnl_by_day[day_key] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_composable_strategy_blocks_entry_on_foreign_strategy_ownership():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_ownership_limit_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_ownership_limit_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._submit_intent = AsyncMock()
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="foreign-lot",
            strategy_id="beta",
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="x",
            entry_order_id=None,
            entry_perm_id=None,
            live_qty=1.0,
            filled_qty=1.0,
            avg_entry_price=50.0,
            state="live",
            lane_id=None,
            ownership_scope="strategy",
            protection_order_refs=[],
            flatten_order_refs=[],
            extras={},
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    strategy._submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_watchdog_flatten_respects_authority():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_watchdog_authority_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_watchdog_authority_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "protection_missing_detect_sec": 1,
            "protection_missing_escalate_sec": 2,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._submit_exit_intent = AsyncMock()

    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-watchdog-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=1.0,
            filled_qty=1.0,
            avg_entry_price=50.0,
            state="live",
            flatten_authority="portfolio",
            protection_order_refs=[],
            flatten_order_refs=[],
            extras={},
        )
    )
    instrument = strategy._instrument_for_signal_symbol("QQQ") or strategy._instrument_for_exec_symbol("TQQQ")
    snapshot_1 = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    snapshot_2 = MarketSnapshot(symbol="QQQ", ts_epoch_s=103.5, last_price=501.0, session="RTH")

    await strategy._check_runtime_safety_invariants(signal_symbol="QQQ", instrument=instrument, snapshot=snapshot_1)
    await strategy._check_runtime_safety_invariants(signal_symbol="QQQ", instrument=instrument, snapshot=snapshot_2)

    strategy._submit_exit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_composable_strategy_flatten_timeout_skips_escalation_when_lot_already_converged():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_flatten_timeout_converged_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_flatten_timeout_converged_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "flatten_convergence_timeout_sec": 90.0,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    instrument = strategy._instrument_for_signal_symbol("QQQ") or strategy._instrument_for_exec_symbol("TQQQ")
    strategy.flatten_completion_verifier.mark_flatten_submitted(
        symbol="TQQQ",
        position_lot_id="ghost-lot",
        now_ts_epoch_s=100.0,
    )

    await strategy._check_runtime_safety_invariants(
        signal_symbol="QQQ",
        instrument=instrument,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=210.0, last_price=500.0, session="RTH"),
    )

    assert "TQQQ" not in strategy._symbol_kill_switch
    assert strategy.order_lifecycle_module.freeze_reason(symbol="TQQQ") is None


@pytest.mark.asyncio
async def test_composable_strategy_flatten_timeout_escalates_when_lot_still_live():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_flatten_timeout_live_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_flatten_timeout_live_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "flatten_convergence_timeout_sec": 90.0,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-timeout-live-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=1.0,
            filled_qty=1.0,
            avg_entry_price=50.0,
            state="pending_exit",
            extras={},
        )
    )
    instrument = strategy._instrument_for_signal_symbol("QQQ") or strategy._instrument_for_exec_symbol("TQQQ")
    strategy.flatten_completion_verifier.mark_flatten_submitted(
        symbol="TQQQ",
        position_lot_id="lot-timeout-live-1",
        now_ts_epoch_s=100.0,
    )

    await strategy._check_runtime_safety_invariants(
        signal_symbol="QQQ",
        instrument=instrument,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=210.0, last_price=500.0, session="RTH"),
    )

    assert strategy._symbol_kill_switch["TQQQ"] == "flatten_not_converged_timeout"
    assert strategy.order_lifecycle_module.freeze_reason(symbol="TQQQ") == "flatten_not_converged_timeout"


@pytest.mark.asyncio
async def test_composable_strategy_executes_multiple_policy_exit_actions_for_distinct_lots():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_multi_exit_actions_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_multi_exit_actions_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._submit_exit_intent = AsyncMock()

    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-exit-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d1",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=50.0,
            state="live",
            extras={},
        )
    )
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-exit-2",
            strategy_id=strategy.strategy_id,
            symbol="SQQQ",
            side="SHORT",
            opened_by_decision_id="d2",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:SQQQ:ENTRY",
            live_qty=3.0,
            filled_qty=3.0,
            avg_entry_price=40.0,
            state="live",
            extras={},
        )
    )

    instrument = strategy._instrument_for_signal_symbol("QQQ") or strategy._instrument_for_exec_symbol("TQQQ")
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=snapshot,
        history=HistoryBundle(symbol="QQQ"),
        values={},
        readiness={},
    )
    triggered = await strategy._execute_policy_exit_actions(
        action_plans=(
            ActionPlan(action_type=ActionType.TAKE_PROFIT, symbol="TQQQ", qty=1.0, position_lot_id="lot-exit-1", priority=10),
            ActionPlan(action_type=ActionType.REDUCE, symbol="SQQQ", qty=2.0, position_lot_id="lot-exit-2", priority=20),
        ),
        instrument=instrument,
        snapshot=snapshot,
        feature_context=feature_context,
    )

    assert triggered is True
    assert strategy._submit_exit_intent.await_count == 2
    submitted_roles = [call.kwargs["exit_plan"].order_role for call in strategy._submit_exit_intent.await_args_list]
    submitted_lot_ids = [call.kwargs["exit_plan"].position_lot_id for call in strategy._submit_exit_intent.await_args_list]
    assert submitted_roles == ["TAKE_PROFIT", "REDUCE"]
    assert submitted_lot_ids == ["lot-exit-1", "lot-exit-2"]


@pytest.mark.asyncio
async def test_composable_strategy_executes_cancel_working_plus_flatten_action_set():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_cancel_working_flatten_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_cancel_working_flatten_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._submit_exit_intent = AsyncMock()

    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-cancel-flat-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d1",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:kernel_test_cancel_working_flatten_001:TQQQ:ENTRY:TRG:abc",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=50.0,
            state="live",
            extras={},
        )
    )
    strategy._open_orders_for_cancel_action = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="TQQQ",
                state="Submitted",
                order_ref="stkv1:kernel_test_cancel_working_flatten_001:TQQQ:ENTRY:TRG:abc",
                order_id="101",
                strategy_id=strategy.strategy_id,
            )
        ]
    )
    cancelled_ids: list[int] = []

    async def _cancel(order_id: int):
        cancelled_ids.append(int(order_id))
        return True

    strategy._resolve_broker_cancel_fn = lambda: _cancel
    instrument = strategy._instrument_for_signal_symbol("QQQ") or strategy._instrument_for_exec_symbol("TQQQ")
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=snapshot,
        history=HistoryBundle(symbol="QQQ"),
        values={},
        readiness={},
    )
    triggered = await strategy._execute_policy_exit_actions(
        action_plans=(
            ActionPlan(action_type=ActionType.CANCEL_WORKING, symbol="TQQQ", position_lot_id="lot-cancel-flat-1", priority=5),
            ActionPlan(action_type=ActionType.FLATTEN, symbol="TQQQ", position_lot_id="lot-cancel-flat-1", qty=2.0, priority=10),
        ),
        instrument=instrument,
        snapshot=snapshot,
        feature_context=feature_context,
    )

    assert triggered is True
    assert cancelled_ids == [101]
    strategy._submit_exit_intent.assert_awaited_once()
    submitted_plan = strategy._submit_exit_intent.await_args.kwargs["exit_plan"]
    assert submitted_plan.order_role == "FLATTEN"
    assert submitted_plan.position_lot_id == "lot-cancel-flat-1"


@pytest.mark.asyncio
async def test_composable_strategy_executes_ensure_protection_action_for_target_lot():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_ensure_protection_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_ensure_protection_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._submit_exit_intent = AsyncMock()

    lot = PositionLot(
        position_lot_id="lot-ensure-protection-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d1",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:kernel_test_ensure_protection_001:TQQQ:ENTRY:TRG:abc",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=50.0,
        state="live",
        protection_order_refs=[],
        extras={
            "protective_oco": {"sl_offset_abs": 1.0, "tp_offset_abs": 2.0},
            "protection_state": {"position_lifecycle": "live", "protection_lifecycle": "repair_pending"},
        },
    )
    strategy.execution.ledger.open_lot(lot)
    strategy._attempt_rebuild_missing_protection_for_lot = AsyncMock(return_value=True)

    instrument = strategy._instrument_for_signal_symbol("QQQ") or strategy._instrument_for_exec_symbol("TQQQ")
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=snapshot,
        history=HistoryBundle(symbol="QQQ"),
        values={},
        readiness={},
    )
    triggered = await strategy._execute_policy_exit_actions(
        action_plans=(
            ActionPlan(action_type=ActionType.ENSURE_PROTECTION, symbol="TQQQ", position_lot_id="lot-ensure-protection-1", priority=5),
        ),
        instrument=instrument,
        snapshot=snapshot,
        feature_context=feature_context,
    )

    assert triggered is True
    strategy._attempt_rebuild_missing_protection_for_lot.assert_awaited_once()
    call_kwargs = strategy._attempt_rebuild_missing_protection_for_lot.await_args.kwargs
    assert call_kwargs["lot"].position_lot_id == "lot-ensure-protection-1"
    assert call_kwargs["reason"] == "policy_action_ensure_protection"
    strategy._submit_exit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_composable_strategy_protection_lifecycle_tracks_entry_fill_tp1_and_trailing():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_protection_lifecycle_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_protection_lifecycle_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    lot = PositionLot(
        position_lot_id="lot-life-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:a:TQQQ:ENTRY",
        live_qty=4.0,
        filled_qty=4.0,
        avg_entry_price=100.0,
        state="live",
        extras={
            "stop_plan": {"atr_eff": 2.0, "logic_stop_offset": 1.5},
            "take_profit_plan": {"atr_eff": 2.0, "target_offset_atr": 1.0},
            "retreat_plan": {"logic_retreat_min_hold_sec": 360},
            "protective_oco": {"sl_offset_abs": 1.5, "tp_offset_abs": 2.0},
        },
    )
    strategy.execution.ledger.open_lot(lot)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates={
            "protection_state": strategy.protection_module.build_initial_position_state(
                signal_symbol="QQQ",
                exec_symbol="TQQQ",
                stop_plan=lot.extras["stop_plan"],
                take_profit_plan=lot.extras["take_profit_plan"],
                retreat_plan=lot.extras["retreat_plan"],
                protective_oco=lot.extras["protective_oco"],
            )
        },
    )
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_entry_fill(
            lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
            fill_price=100.0,
            now_ts_epoch_s=100.0,
        ),
    )
    live_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    live_state = dict(live_lot.extras["protection_state"])

    assert live_state["position_lifecycle"] == "live"
    assert live_state["target_price"] == pytest.approx(102.0)
    assert live_state["active_stop_price"] == pytest.approx(98.5)

    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=101.0, last_price=500.0, session="RTH"),
        history=HistoryBundle(symbol="QQQ"),
        values={"atr_eff": 2.0, "v3m": 0.02},
        readiness={},
    )
    tp1_plan = strategy._select_exit_plan_for_lot(
        lot=live_lot,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=101.0, last_price=103.0, session="RTH"),
        feature_context=feature_context,
    )

    assert tp1_plan is not None
    assert tp1_plan.order_role == "TAKE_PROFIT"
    assert tp1_plan.payload["reason"] == "tp1_hit"
    assert tp1_plan.qty == pytest.approx(2.0)

    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_exit_intent(
            lot=live_lot,
            exit_plan=tp1_plan,
            now_ts_epoch_s=101.0,
        ),
    )
    strategy.execution.ledger.record_exit_fill(position_lot_id=lot.position_lot_id, fill_qty=2.0)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_exit_fill(
            lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
            fill_qty=2.0,
            fill_price=103.0,
            now_ts_epoch_s=101.5,
        ),
    )

    post_tp1_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    post_tp1_state = dict(post_tp1_lot.extras["protection_state"])
    assert post_tp1_state["position_lifecycle"] == "post_tp1"
    assert post_tp1_state["tp1_done"] is True

    trailing_plan = strategy._select_exit_plan_for_lot(
        lot=post_tp1_lot,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=102.0, last_price=105.0, session="RTH"),
        feature_context=feature_context,
    )
    assert trailing_plan is None

    trailing_stop_plan = strategy._select_exit_plan_for_lot(
        lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=103.0, last_price=103.8, session="RTH"),
        feature_context=feature_context,
    )
    assert trailing_stop_plan is not None
    assert trailing_stop_plan.order_role == "FLATTEN"
    assert trailing_stop_plan.payload["reason"] == "tp1_then_trailing_stop"


@pytest.mark.asyncio
async def test_composable_strategy_protection_lifecycle_arms_breakeven_and_widens_dynamic_stop():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_protection_adjust_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_protection_adjust_001",
        extras={
            "enable_breakeven_shield": True,
            "enable_dynamic_stop_widen": True,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    lot = PositionLot(
        position_lot_id="lot-life-2",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:a:TQQQ:ENTRY",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=100.0,
        state="live",
        extras={
            "stop_plan": {"atr_eff": 2.0, "logic_stop_offset": 1.0},
            "take_profit_plan": {"atr_eff": 2.0, "target_offset_atr": 1.0},
            "protective_oco": {"sl_offset_abs": 1.0, "tp_offset_abs": 2.0},
        },
    )
    strategy.execution.ledger.open_lot(lot)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates={
            "protection_state": strategy.protection_module.build_initial_position_state(
                signal_symbol="QQQ",
                exec_symbol="TQQQ",
                stop_plan=lot.extras["stop_plan"],
                take_profit_plan=lot.extras["take_profit_plan"],
                retreat_plan=None,
                protective_oco=lot.extras["protective_oco"],
            )
        },
    )
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_entry_fill(
            lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
            fill_price=100.0,
            now_ts_epoch_s=100.0,
        ),
    )
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=102.0, last_price=500.0, session="RTH"),
        history=HistoryBundle(symbol="QQQ"),
        values={"atr_eff": 2.0, "v3m": 0.02},
        readiness={},
    )

    no_exit = strategy._select_exit_plan_for_lot(
        lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=102.0, last_price=101.5, session="RTH"),
        feature_context=feature_context,
    )
    adjusted_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    adjusted_state = dict(adjusted_lot.extras["protection_state"])

    assert no_exit is None
    assert adjusted_state["breakeven_armed"] is True
    assert adjusted_state["active_stop_price"] == pytest.approx(100.2)
    assert adjusted_state["management_phase"] == "pre_tp1_breakeven"

    adjusted_state["breakeven_armed"] = False
    adjusted_state["dynamic_stop_last_ts_epoch_s"] = 0.0
    adjusted_state["active_stop_price"] = 99.0
    adjusted_state["hard_stop_price"] = 99.0
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates={"protection_state": adjusted_state},
    )
    no_exit = strategy._select_exit_plan_for_lot(
        lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=406.0, last_price=100.5, session="RTH"),
        feature_context=feature_context,
    )
    widened_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    widened_state = dict(widened_lot.extras["protection_state"])

    assert no_exit is None
    assert widened_state["dynamic_stop_updates"] >= 1
    assert widened_state["active_stop_price"] < 99.0
    assert widened_state["management_phase"] == "dynamic_stop_adjusting"


@pytest.mark.asyncio
async def test_composable_strategy_protection_lifecycle_supports_optional_tp2_contract():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_protection_tp2_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_protection_tp2_001",
        extras={
            "enable_tp2_lifecycle": True,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    lot = PositionLot(
        position_lot_id="lot-life-tp2-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:a:TQQQ:ENTRY",
        live_qty=4.0,
        filled_qty=4.0,
        avg_entry_price=100.0,
        state="live",
        extras={
            "stop_plan": {"atr_eff": 2.0, "logic_stop_offset": 1.5},
            "take_profit_plan": {"atr_eff": 2.0, "target_offset_atr": 1.0},
            "protective_oco": {"sl_offset_abs": 1.5, "tp_offset_abs": 2.0},
        },
    )
    strategy.execution.ledger.open_lot(lot)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates={
            "protection_state": strategy.protection_module.build_initial_position_state(
                signal_symbol="QQQ",
                exec_symbol="TQQQ",
                stop_plan=lot.extras["stop_plan"],
                take_profit_plan=lot.extras["take_profit_plan"],
                retreat_plan=None,
                protective_oco=lot.extras["protective_oco"],
            )
        },
    )
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_entry_fill(
            lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
            fill_price=100.0,
            now_ts_epoch_s=100.0,
        ),
    )
    live_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    live_state = dict(live_lot.extras["protection_state"])
    assert live_state["tp2_target_price"] == pytest.approx(104.0)

    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=101.0, last_price=500.0, session="RTH"),
        history=HistoryBundle(symbol="QQQ"),
        values={"atr_eff": 2.0, "v3m": 0.02},
        readiness={},
    )
    tp1_plan = strategy._select_exit_plan_for_lot(
        lot=live_lot,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=101.0, last_price=103.0, session="RTH"),
        feature_context=feature_context,
    )
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_exit_intent(
            lot=live_lot,
            exit_plan=tp1_plan,
            now_ts_epoch_s=101.0,
        ),
    )
    strategy.execution.ledger.record_exit_fill(position_lot_id=lot.position_lot_id, fill_qty=2.0)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_exit_fill(
            lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
            fill_qty=2.0,
            fill_price=103.0,
            now_ts_epoch_s=101.5,
        ),
    )

    post_tp1_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    tp2_plan = strategy._select_exit_plan_for_lot(
        lot=post_tp1_lot,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=102.0, last_price=104.5, session="RTH"),
        feature_context=feature_context,
    )
    assert tp2_plan is not None
    assert tp2_plan.order_role == "TAKE_PROFIT"
    assert tp2_plan.payload["reason"] == "tp2_hit"
    assert tp2_plan.qty == pytest.approx(2.0)

    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_exit_intent(
            lot=post_tp1_lot,
            exit_plan=tp2_plan,
            now_ts_epoch_s=102.0,
        ),
    )
    strategy.execution.ledger.record_exit_fill(position_lot_id=lot.position_lot_id, fill_qty=2.0)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates=strategy.protection_module.on_exit_fill(
            lot=strategy.execution.ledger.get_lot(lot.position_lot_id),
            fill_qty=2.0,
            fill_price=104.5,
            now_ts_epoch_s=102.5,
        ),
    )
    closed_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    closed_state = dict(closed_lot.extras["protection_state"])
    assert closed_state["tp2_done"] is True
    assert closed_state["position_lifecycle"] == "closed"
    assert closed_state["protection_lifecycle"] == "closed"


@pytest.mark.asyncio
async def test_composable_strategy_recovery_rebuilds_missing_protection_stop():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_restart_rebuild_001",
    )
    submit_calls = []

    async def _submit_intent(envelope):
        submit_calls.append(dict(envelope.extras))
        return {"success": True, "status": "APPROVED", "order_id": "stop-1", "perm_id": 901}

    ctx.submit_intent = _submit_intent
    params = SimpleNamespace(
        strategy_id="kernel_test_restart_rebuild_001",
        extras={
            "order_namespace": "stkv1",
            "startup_preflight_enabled": False,
            "dry_run": False,
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._remember_latest_snapshot(symbol="QQQ", features={"ts_epoch_s": 100.0, "last_price": 500.0, "session": "RTH"})

    lot = PositionLot(
        position_lot_id="lot-restart-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:a:TQQQ:ENTRY",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=100.0,
        state="live",
        extras={
            "protective_oco": {"sl_offset_abs": 1.0, "tp_offset_abs": 2.0},
            "protection_state": {
                "position_lifecycle": "live",
                "protection_lifecycle": "pending_attach",
                "management_phase": "pre_tp1",
            },
        },
    )
    strategy.execution.ledger.open_lot(lot)

    ready = await strategy.recovery_module.ensure_runtime_ready_for_tick(symbol="QQQ")
    rebuilt_lot = strategy.execution.ledger.get_lot("lot-restart-1")

    assert ready is True
    assert len(submit_calls) == 1
    assert submit_calls[0]["exit_order_role"] == "STOP"
    assert submit_calls[0]["protective_rebuild"] is True
    assert rebuilt_lot is not None
    assert rebuilt_lot.protection_order_refs
    assert rebuilt_lot.extras["protection_state"]["protection_lifecycle"] == "protected"


@pytest.mark.asyncio
async def test_composable_strategy_rebuild_helper_submits_missing_protection_stop():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_watchdog_rebuild_001",
    )
    submit_calls = []

    async def _submit_intent(envelope):
        submit_calls.append(dict(envelope.extras))
        return {"success": True, "status": "APPROVED", "order_id": "stop-2", "perm_id": 902}

    ctx.submit_intent = _submit_intent
    params = SimpleNamespace(
        strategy_id="kernel_test_watchdog_rebuild_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "protection_missing_detect_sec": 1,
            "protection_missing_escalate_sec": 3,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    lot = PositionLot(
        position_lot_id="lot-watchdog-rebuild-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:a:TQQQ:ENTRY",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=100.0,
        state="live",
        extras={
            "protective_oco": {"sl_offset_abs": 1.0, "tp_offset_abs": 2.0},
            "protection_state": {
                "position_lifecycle": "live",
                "protection_lifecycle": "pending_attach",
                "management_phase": "pre_tp1",
            },
        },
    )
    strategy.execution.ledger.open_lot(lot)
    rebuilt = await strategy._attempt_rebuild_missing_protection_for_lot(
        lot=lot,
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=101.5, last_price=501.0, session="RTH"),
        reason="rebuild_stop_after_missing_protection",
    )

    rebuilt_lot = strategy.execution.ledger.get_lot("lot-watchdog-rebuild-1")
    assert rebuilt is True
    assert len(submit_calls) == 1
    assert submit_calls[0]["exit_order_role"] == "STOP"
    assert rebuilt_lot is not None
    assert rebuilt_lot.extras["protection_state"]["protection_lifecycle"] == "protected"


@pytest.mark.asyncio
async def test_composable_strategy_missing_protection_escalation_marks_flatten_only_state():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_watchdog_flatten_only_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_watchdog_flatten_only_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "protection_missing_detect_sec": 1,
            "protection_missing_escalate_sec": 2,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    lot = PositionLot(
        position_lot_id="lot-watchdog-flat-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d",
        opened_by_trigger_id=None,
        entry_order_ref="stkv1:a:TQQQ:ENTRY",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=100.0,
        state="live",
        extras={
            "protection_state": {
                "position_lifecycle": "live",
                "protection_lifecycle": "pending_attach",
                "management_phase": "pre_tp1",
            },
        },
    )
    strategy.execution.ledger.open_lot(lot)
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id="lot-watchdog-flat-1",
        updates=strategy.protection_module.mark_protection_missing(
            lot=lot,
            now_ts_epoch_s=103.5,
            escalate=True,
        ),
    )

    degraded_lot = strategy.execution.ledger.get_lot("lot-watchdog-flat-1")
    assert degraded_lot is not None
    assert degraded_lot.extras["protection_state"]["protection_lifecycle"] == "flatten_only"


@pytest.mark.asyncio
async def test_composable_strategy_entry_frozen_when_live_lot_requires_protection_repair():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_entry_freeze_repair_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_entry_freeze_repair_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-freeze-repair-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=100.0,
            state="live",
            extras={
                "protection_state": {
                    "position_lifecycle": "live",
                    "protection_lifecycle": "repair_pending",
                    "management_phase": "pre_tp1",
                    "freeze_required": True,
                }
            },
        )
    )

    assert strategy._entry_frozen_for_action_arbiter(signal_symbol="QQQ", instrument=strategy.config.instruments[0]) is True


@pytest.mark.asyncio
async def test_composable_strategy_restart_recovery_flattens_unprotected_live_lot_without_template():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_restart_flatten_001",
    )
    submit_calls = []

    async def _submit_intent(envelope):
        submit_calls.append(dict(envelope.extras))
        return {"success": True, "status": "APPROVED", "order_id": "flatten-1", "perm_id": 911}

    ctx.submit_intent = _submit_intent
    params = SimpleNamespace(
        strategy_id="kernel_test_restart_flatten_001",
        extras={
            "order_namespace": "stkv1",
            "startup_preflight_enabled": False,
            "dry_run": False,
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._remember_latest_snapshot(symbol="QQQ", features={"ts_epoch_s": 100.0, "last_price": 500.0, "session": "RTH"})
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-restart-flat-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=100.0,
            state="live",
            extras={
                "protection_state": {
                    "position_lifecycle": "live",
                    "protection_lifecycle": "flatten_only",
                    "management_phase": "pre_tp1",
                    "freeze_required": True,
                    "degrade_reason": "unsafe_position_without_protection",
                }
            },
        )
    )

    ready = await strategy.recovery_module.ensure_runtime_ready_for_tick(symbol="QQQ")

    assert ready is True
    assert len(submit_calls) == 1
    assert submit_calls[0]["exit_order_role"] == "FLATTEN"
    assert submit_calls[0]["restart_recovery"] is True
    assert strategy._symbol_kill_switch["TQQQ"] == "unsafe_position_without_protection"


@pytest.mark.asyncio
async def test_composable_strategy_position_update_reconcile_respects_adopt_authority():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_position_reconcile_authority_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_position_reconcile_authority_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-reconcile-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=50.0,
            state="live",
            adopt_authority="portfolio",
            protection_order_refs=[],
            flatten_order_refs=[],
            extras={},
        )
    )

    await strategy.on_position_update(
        SimpleNamespace(
            symbol="TQQQ",
            pos_qty=1.0,
            avg_px=51.0,
            realized_pnl_usd=0.0,
            unrealized_pnl_usd=0.0,
        )
    )

    lot = strategy.execution.ledger.get_lot("lot-reconcile-1")
    assert lot is not None
    assert float(lot.live_qty) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_composable_strategy_position_update_reconcile_emits_authority_granted_audit():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_position_reconcile_authority_granted_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_position_reconcile_authority_granted_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    events: list[tuple[str, str]] = []

    def _capture(stage: str, symbol: str, reason: str, payload):
        events.append((stage, reason))

    strategy._audit = _capture
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-reconcile-granted-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:a:TQQQ:ENTRY",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=50.0,
            state="live",
            adopt_authority="strategy",
            extras={},
        )
    )

    await strategy.on_position_update(
        SimpleNamespace(
            symbol="TQQQ",
            pos_qty=1.0,
            avg_px=51.0,
            realized_pnl_usd=0.0,
            unrealized_pnl_usd=0.0,
        )
    )

    assert ("position_reconcile_authority_granted", "ready") in events


@pytest.mark.asyncio
async def test_composable_strategy_exit_intent_blocks_lane_authority_mismatch():
    submit_calls: list[dict] = []

    async def _submit_intent(envelope):
        submit_calls.append({"symbol": envelope.symbol, "extras": dict(envelope.extras)})
        return {"ok": True, "broker_order_id": "301", "perm_id": 301}

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_exit_lane_authority_001",
        submit_intent=_submit_intent,
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_exit_lane_authority_001",
        extras={
            "dry_run": False,
            "order_namespace": "stkv1",
            "startup_preflight_enabled": False,
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    lot = PositionLot(
        position_lot_id="lot-lane-auth-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d1",
        opened_by_trigger_id="trg1",
        entry_order_ref="stkv1:kernel_test_exit_lane_authority_001:TQQQ:ENTRY:LOT:lot-lane-auth-1",
        lane_id="lane_a",
        ownership_scope="lane",
        flatten_authority="lane",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=100.0,
        state="live",
    )
    strategy.execution.ledger.open_lot(lot)
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH", vwap=499.0)

    await strategy._submit_exit_intent(
        exit_plan=ExitPlan(
            symbol="TQQQ",
            order_role="FLATTEN",
            qty=2.0,
            position_lot_id=lot.position_lot_id,
            lane_id="lane_b",
            ownership_scope="lane",
            payload={"reason": "lane_mismatch_test"},
        ),
        snapshot=snapshot,
        lot=lot,
    )

    assert len(submit_calls) == 0


@pytest.mark.asyncio
async def test_composable_strategy_injects_runtime_pending_order_count():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_runtime_guard_001",
    )
    params = SimpleNamespace(strategy_id="kernel_test_runtime_guard_001", extras={})
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._same_strategy_pending_orders = lambda exec_symbols: __import__("asyncio").sleep(0, result=3)

    features = await strategy._inject_runtime_guards(
        symbol="QQQ",
        instrument=SimpleNamespace(long_exec_symbol="TQQQ", short_exec_symbol="SQQQ"),
        features={"same_strategy_pending_orders": 0},
    )

    assert features["same_strategy_pending_orders"] == 3


@pytest.mark.asyncio
async def test_composable_strategy_runtime_guards_override_stale_upstream_pending_count():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_runtime_guard_override_001",
    )
    params = SimpleNamespace(strategy_id="kernel_test_runtime_guard_override_001", extras={})
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    strategy._same_strategy_pending_orders = lambda exec_symbols: __import__("asyncio").sleep(0, result=0)

    features = await strategy._inject_runtime_guards(
        symbol="QQQ",
        instrument=SimpleNamespace(long_exec_symbol="TQQQ", short_exec_symbol="SQQQ"),
        features={"same_strategy_pending_orders": 2},
    )

    assert features["same_strategy_pending_orders"] == 0


def test_composable_strategy_entry_plan_from_action_plans_prefers_first_valid():
    instrument = SimpleNamespace(
        signal_symbol="QQQ",
        long_exec_symbol="TQQQ",
        short_exec_symbol="SQQQ",
        execution_lanes=(),
        lane_for_side=lambda side: None,
        exec_symbols=lambda: ("TQQQ", "SQQQ"),
    )
    fallback = EntryPlan(symbol="SQQQ", side="SHORT", qty=1.0, trigger_id="fallback", payload={})
    plans = (
        ActionPlan(action_type=ActionType.FLATTEN, symbol="QQQ", priority=10),
        ActionPlan(action_type=ActionType.ENTRY, symbol="QQQ", side="LONG", qty=5.0, priority=20, trigger_id="a1"),
        ActionPlan(action_type=ActionType.ENTRY, symbol="QQQ", side="SHORT", qty=4.0, priority=30, trigger_id="a2"),
    )

    out = ComposableStocksKernelStrategy._entry_plan_from_action_plans(
        action_plans=plans,
        instrument=instrument,
        snapshot_symbol="QQQ",
        fallback=fallback,
    )

    assert out is not None
    assert out.side == "LONG"
    assert out.symbol == "TQQQ"
    assert out.qty == pytest.approx(5.0)


def test_composable_strategy_entry_plans_from_action_plans_supports_multi_entry_set():
    instrument = SimpleNamespace(
        signal_symbol="QQQ",
        long_exec_symbol="TQQQ",
        short_exec_symbol="SQQQ",
        execution_lanes=(),
        lane_for_side=lambda side: None,
        exec_symbols=lambda: ("TQQQ", "SQQQ"),
    )
    plans = (
        ActionPlan(action_type=ActionType.ENTRY, symbol="QQQ", side="LONG", qty=3.0, priority=10, trigger_id="e1"),
        ActionPlan(action_type=ActionType.ADD, symbol="QQQ", side="LONG", qty=1.0, priority=20, trigger_id="a1"),
        ActionPlan(action_type=ActionType.ADD, symbol="QQQ", side="LONG", qty=1.0, priority=21, trigger_id="a1"),
    )

    out = ComposableStocksKernelStrategy._entry_plans_from_action_plans(
        action_plans=plans,
        instrument=instrument,
        snapshot_symbol="QQQ",
        fallback=None,
    )

    assert len(out) == 2
    assert out[0].symbol == "TQQQ"
    assert out[0].qty == pytest.approx(3.0)
    assert out[0].payload["action_type"] == "ENTRY"
    assert out[1].symbol == "TQQQ"
    assert out[1].qty == pytest.approx(1.0)
    assert out[1].payload["action_type"] == "ADD"


@pytest.mark.asyncio
async def test_composable_strategy_entry_submission_executes_entry_action_set_in_one_tick():
    submitted_decisions: list[str] = []

    async def _submit_intent(envelope):
        submitted_decisions.append(str(envelope.decision_id))
        return {"accepted": True}

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_entry_action_set_001",
        submit_intent=_submit_intent,
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_entry_action_set_001",
        extras={
            "order_namespace": "stkv1",
            "startup_preflight_enabled": False,
            "dry_run": False,
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    ts_epoch_s = datetime(2026, 4, 1, 9, 35, tzinfo=ET).timestamp()
    strategy._reap_stale_pending_entries = AsyncMock(return_value=None)
    strategy._ensure_exec_symbol_snapshot = AsyncMock(
        return_value=MarketSnapshot(symbol="TQQQ", ts_epoch_s=ts_epoch_s, last_price=50.0, session="RTH")
    )
    instrument = strategy.config.instruments[0]
    runtime_state = {
        "snapshot": MarketSnapshot(symbol="QQQ", ts_epoch_s=ts_epoch_s, last_price=500.0, session="RTH"),
        "readiness": SimpleNamespace(
            ready=True,
            reason="ready",
            freshness_ok=True,
            history_ready=True,
            snapshot_ready=True,
            payload={},
        ),
        "feature_context": FeatureContext(
            symbol="QQQ",
            snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=ts_epoch_s, last_price=500.0, session="RTH"),
            history=HistoryBundle(symbol="QQQ"),
            values={"v3m": 0.02, "atr_eff": 1.0},
            readiness={},
        ),
        "gate_report": GateEvaluationReport(allowed=True, decisions=()),
        "signal": SignalIntent(action="enter", side="LONG", symbol="QQQ"),
        "entry_plan": EntryPlan(symbol="TQQQ", side="LONG", qty=1.0, trigger_id="fallback"),
        "entry_plans": (
            EntryPlan(symbol="TQQQ", side="LONG", qty=1.0, trigger_id="e1", payload={"action_type": "ENTRY"}),
            EntryPlan(symbol="TQQQ", side="LONG", qty=0.5, trigger_id="a1", payload={"action_type": "ADD"}),
        ),
        "stop_plan": None,
        "take_profit_plan": None,
        "retreat_plan": None,
        "instrument": instrument,
    }

    await strategy._runtime_stage_entry_submission(runtime_state)
    assert len(submitted_decisions) == 2
    assert submitted_decisions[0] != submitted_decisions[1]
    active_lots = tuple(strategy.execution.ledger.list_lots(strategy_id=strategy.strategy_id))
    assert len(active_lots) == 2
    assert all(str(getattr(lot, "state", "")) == "pending_entry" for lot in active_lots)


def test_composable_strategy_resolve_action_exec_symbol_prefers_lane_id():
    instrument = SimpleNamespace(
        signal_symbol="QQQ",
        long_exec_symbol="TQQQ",
        short_exec_symbol="SQQQ",
        execution_lanes=(SimpleNamespace(lane_id="long_lane", symbol="UPRO"),),
        lane_for_side=lambda side: None,
        exec_symbols=lambda: ("UPRO", "SQQQ"),
    )
    action = ActionPlan(action_type=ActionType.ENTRY, symbol="QQQ", side="LONG", qty=1.0, lane_id="long_lane")

    symbol = ComposableStocksKernelStrategy._resolve_action_exec_symbol(
        action=action,
        instrument=instrument,
        side="LONG",
        snapshot_symbol="QQQ",
    )

    assert symbol == "UPRO"


def test_composable_strategy_action_graph_blocks_add_without_entry_by_default_contract():
    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None), metrics=SimpleNamespace(), engine=SimpleNamespace(), strategy_id="kernel_graph_default_001"),
        params=SimpleNamespace(strategy_id="kernel_graph_default_001", extras={}),
    )
    winners, blocked = strategy._apply_action_graph_contracts(
        action_plans=(
            ActionPlan(action_type=ActionType.ADD, symbol="TQQQ", side="LONG", qty=1.0, position_lot_id="lot-1"),
        ),
    )

    assert winners == ()
    assert len(blocked) == 1
    assert blocked[0].action_type == ActionType.ADD


def test_composable_strategy_action_graph_custom_suppression_replaces_prior_winner_in_scope():
    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None), metrics=SimpleNamespace(), engine=SimpleNamespace(), strategy_id="kernel_graph_custom_001"),
        params=SimpleNamespace(strategy_id="kernel_graph_custom_001", extras={}),
    )
    strategy.config = SimpleNamespace(
        raw_extras={
            "action_graph": {
                "requires": {},
                "suppresses": {"ENSURE_PROTECTION": ["TAKE_PROFIT"]},
            }
        }
    )
    winners, blocked = strategy._apply_action_graph_contracts(
        action_plans=(
            ActionPlan(action_type=ActionType.TAKE_PROFIT, symbol="TQQQ", qty=1.0, position_lot_id="lot-1", priority=10),
            ActionPlan(action_type=ActionType.ENSURE_PROTECTION, symbol="TQQQ", position_lot_id="lot-1", priority=20),
        ),
    )

    assert [item.action_type for item in winners] == [ActionType.ENSURE_PROTECTION]
    assert [item.action_type for item in blocked] == [ActionType.TAKE_PROFIT]


@pytest.mark.asyncio
async def test_composable_strategy_clears_stale_pending_entry_when_broker_and_db_are_clean():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_runtime_pending_cleanup_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_runtime_pending_cleanup_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._resolve_pg = lambda: SimpleNamespace(fetchrow=AsyncMock(return_value={"pending_count": 0}))

    submit = strategy.execution.open_entry_lot(
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )

    pending = await strategy._same_strategy_pending_orders(exec_symbols=["TQQQ"])
    lot = strategy.execution.ledger.get_lot(submit.position_lot_id)
    detail = strategy._last_same_strategy_pending_detail[("TQQQ",)]

    assert pending == 0
    assert lot is not None and lot.state == "quarantined"
    assert detail["runtime_cleanup"][0]["action"] == "quarantined_from_broker_db_clean"


@pytest.mark.asyncio
async def test_composable_strategy_reconciles_pending_entry_from_filled_db_position():
    class _Pg:
        async def fetchrow(self, *_args, **_kwargs):
            return {"pending_count": 0}

        async def fetch(self, sql, *args):
            assert "FROM core.position_ledger_current" in str(sql)
            assert "FROM core.core_position" not in str(sql)
            assert args[1] == "kernel_test_runtime_pending_db_position_001"
            assert opened.position_lot_id in args[3]
            assert opened.order_ref in args[4]
            return [
                {
                    "account_id": "DUO112752",
                    "strategy_id": "kernel_test_runtime_pending_db_position_001",
                    "strategy_instance_id": "kernel_test_runtime_pending_db_position_001",
                    "symbol": "TQQQ",
                    "lot_id": opened.position_lot_id,
                    "ledger_qty": 1.0,
                    "avg_px": 101.25,
                    "order_ref": opened.order_ref,
                    "broker_order_id": "11",
                    "ib_perm_id": 123,
                    "order_qty": 1.0,
                    "limit_px": 101.25,
                }
            ]

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_runtime_pending_db_position_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_runtime_pending_db_position_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._resolve_pg = lambda: _Pg()

    opened = strategy.execution.open_entry_lot(
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )
    strategy.order_lifecycle_module.register_created(
        order_ref=opened.order_ref,
        symbol="TQQQ",
        position_lot_id=opened.position_lot_id,
        now_ts_epoch_s=100.0,
    )

    pending = await strategy._same_strategy_pending_orders(exec_symbols=["TQQQ"])
    lot = strategy.execution.ledger.get_lot(opened.position_lot_id)
    detail = strategy._last_same_strategy_pending_detail[("TQQQ",)]

    assert pending == 0
    assert lot is not None and lot.state == "live"
    assert float(lot.live_qty or 0.0) == 1.0
    assert float(lot.avg_entry_price or 0.0) == 101.25
    assert detail["runtime_db_position_reconcile"][0]["action"] == "live_from_db_position"
    assert detail["runtime_db_position_reconcile"][0]["account_id"] == "DUO112752"
    assert detail["runtime_db_position_reconcile"][0]["strategy_instance_id"] == strategy.strategy_id


@pytest.mark.asyncio
async def test_composable_strategy_does_not_reconcile_pending_entry_without_matching_owner_lane():
    class _Pg:
        async def fetchrow(self, *_args, **_kwargs):
            return {"pending_count": 0}

        async def fetch(self, sql, *args):
            assert "FROM core.position_ledger_current" in str(sql)
            assert "FROM core.core_position" not in str(sql)
            return [
                {
                    "account_id": "DUO112752",
                    "strategy_id": "kernel_test_runtime_pending_wrong_lane_001",
                    "strategy_instance_id": "other_instance",
                    "symbol": "TQQQ",
                    "lot_id": "other-lot-id",
                    "ledger_qty": 1.0,
                    "avg_px": 101.25,
                    "order_ref": opened.order_ref,
                    "broker_order_id": "11",
                    "ib_perm_id": 123,
                    "order_qty": 1.0,
                    "limit_px": 101.25,
                }
            ]

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_runtime_pending_wrong_lane_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_runtime_pending_wrong_lane_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._resolve_pg = lambda: _Pg()

    opened = strategy.execution.open_entry_lot(
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )
    strategy.order_lifecycle_module.register_created(
        order_ref=opened.order_ref,
        symbol="TQQQ",
        position_lot_id=opened.position_lot_id,
        now_ts_epoch_s=100.0,
    )

    actions = await strategy._reconcile_pending_entry_lots_from_db_position(
        exec_symbols=["TQQQ"],
        now_ts_epoch_s=120.0,
    )
    lot = strategy.execution.ledger.get_lot(opened.position_lot_id)

    assert actions == []
    assert lot is not None and lot.state == "pending_entry"


@pytest.mark.asyncio
async def test_composable_strategy_closes_stale_pending_exit_when_broker_and_db_are_clean():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_runtime_pending_exit_cleanup_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_runtime_pending_exit_cleanup_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._resolve_pg = lambda: SimpleNamespace(fetchrow=AsyncMock(return_value={"pending_count": 0}))

    submit = strategy.execution.open_entry_lot(
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )
    strategy.execution.ledger.record_entry_fill(
        position_lot_id=submit.position_lot_id,
        fill_qty=10.0,
        fill_price=100.0,
        broker_order_id="11",
        perm_id=11,
    )
    strategy.execution.ledger.request_exit(
        position_lot_id=submit.position_lot_id,
        flatten_order_ref="stkv1:test:flatten",
    )

    pending = await strategy._same_strategy_pending_orders(exec_symbols=["TQQQ"])
    lot = strategy.execution.ledger.get_lot(submit.position_lot_id)
    detail = strategy._last_same_strategy_pending_detail[("TQQQ",)]

    assert pending == 0
    assert lot is not None and lot.state == "closed"
    assert float(lot.live_qty or 0.0) == 0.0
    assert detail["runtime_cleanup"][0]["action"] == "closed_from_broker_db_clean"


@pytest.mark.asyncio
async def test_composable_strategy_does_not_cleanup_pending_lot_when_db_probe_fails():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_runtime_pending_db_unknown_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_runtime_pending_db_unknown_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._resolve_pg = lambda: SimpleNamespace(fetchrow=AsyncMock(side_effect=RuntimeError("db unavailable")))

    submit = strategy.execution.open_entry_lot(
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )

    pending = await strategy._same_strategy_pending_orders(exec_symbols=["TQQQ"])
    lot = strategy.execution.ledger.get_lot(submit.position_lot_id)
    detail = strategy._last_same_strategy_pending_detail[("TQQQ",)]

    assert pending == 1
    assert lot is not None and lot.state == "pending_entry"
    assert detail["db_pending_known"] is False
    assert "runtime_cleanup" not in detail


@pytest.mark.asyncio
async def test_composable_strategy_db_position_sync_skips_when_adopt_authority_denied():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_db_sync_authority_block_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_db_sync_authority_block_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    pg = SimpleNamespace(fetchrow=AsyncMock(return_value={"qty": 9.0, "avg_px": 101.0}))
    strategy._resolve_pg = lambda: pg

    lot = PositionLot(
        position_lot_id="lot-db-sync-authority-block-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d1",
        opened_by_trigger_id="t1",
        entry_order_ref="stkv1:test:TQQQ:ENTRY",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=100.0,
        state="live",
        adopt_authority="account",
    )
    strategy.execution.ledger.open_lot(lot)

    audits: list[tuple[str, str]] = []
    strategy._audit = lambda stage, symbol, reason, payload: audits.append((str(stage), str(reason)))

    await strategy._sync_db_position(symbol="TQQQ", now_ts_epoch_s=180.0)

    pg.fetchrow.assert_not_awaited()
    after = strategy.execution.ledger.get_lot("lot-db-sync-authority-block-1")
    assert after is not None
    assert float(after.live_qty or 0.0) == pytest.approx(2.0)
    assert ("db_position_sync_skipped", "adopt_authority_reserved") in audits


@pytest.mark.asyncio
async def test_composable_strategy_db_position_sync_reconciles_when_adopt_authority_allowed():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_db_sync_authority_allow_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_db_sync_authority_allow_001",
        extras={
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    async def _fetchrow(sql, *args):
        assert "FROM core.position_ledger_current" in str(sql)
        assert "FROM core.core_position" not in str(sql)
        assert args[1] == "kernel_test_db_sync_authority_allow_001"
        assert args[3] == "lot-db-sync-authority-allow-1"
        return {
            "account_id": "DUO112752",
            "strategy_id": "kernel_test_db_sync_authority_allow_001",
            "strategy_instance_id": "kernel_test_db_sync_authority_allow_001",
            "symbol": "TQQQ",
            "lot_id": "lot-db-sync-authority-allow-1",
            "qty": 3.0,
            "avg_px": 102.5,
        }

    pg = SimpleNamespace(fetchrow=AsyncMock(side_effect=_fetchrow))
    strategy._resolve_pg = lambda: pg

    lot = PositionLot(
        position_lot_id="lot-db-sync-authority-allow-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d1",
        opened_by_trigger_id="t1",
        entry_order_ref="stkv1:test:TQQQ:ENTRY",
        live_qty=1.0,
        filled_qty=1.0,
        avg_entry_price=100.0,
        state="live",
        adopt_authority="strategy",
    )
    strategy.execution.ledger.open_lot(lot)

    await strategy._sync_db_position(symbol="TQQQ", now_ts_epoch_s=180.0)

    pg.fetchrow.assert_awaited_once()
    after = strategy.execution.ledger.get_lot("lot-db-sync-authority-allow-1")
    assert after is not None
    assert float(after.live_qty or 0.0) == pytest.approx(3.0)
    assert float(after.avg_entry_price or 0.0) == pytest.approx(102.5)


@pytest.mark.asyncio
async def test_composable_strategy_binding_broker_isolation_blocks_opposite_leg_when_sibling_has_position():
    broker = _BrokerStub()

    async def _position_qty(symbol):
        return {"SOXL": 0.0, "SOXS": -253.0}.get(str(symbol).upper(), 0.0)

    broker.position_qty = AsyncMock(side_effect=_position_qty)
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_binding_isolation_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_binding_isolation_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    isolation = await strategy._binding_broker_account_isolation(
        signal_symbol="SMH",
        exec_symbol="SOXL",
    )

    assert isolation["blocked"] is True
    assert isolation["reason"] == "broker_binding_live_position_present"
    assert isolation["broker_qty"] == pytest.approx(253.0)
    assert isolation["blocked_symbols"] == [
        {
            "symbol": "SOXS",
            "reason": "broker_live_position_present",
            "broker_qty": -253.0,
            "open_order_count": 0,
            "open_order_refs": [],
        }
    ]


@pytest.mark.asyncio
async def test_composable_strategy_short_circuits_exec_symbol_features_without_active_lot():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_exec_symbol_state_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_exec_symbol_state_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["POST"]}}},
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    await strategy.on_features(
        "TQQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 50.0,
            "session": "POST",
            "vwap": 49.5,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=40 + i, high=41 + i, low=39 + i, close=40 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert "TQQQ" not in strategy.kernel_state
    exec_snapshot = strategy._resolve_exec_snapshot(exec_symbol="TQQQ")
    assert exec_snapshot is not None
    assert exec_snapshot.last_price == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_composable_strategy_recovers_after_exec_broker_adapter_becomes_ready():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=None),
        strategy_id="kernel_test_startup_recover_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_startup_recover_001",
        extras={
            "startup_preflight_enabled": True,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._startup_block_reason = "broker_adapter_unavailable"
    strategy._startup_block_payload = {"reason": "broker_adapter_unavailable", "ok": False}

    ctx.engine.broker_adapter = _BrokerStub()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy._startup_block_reason is None
    assert "QQQ" in strategy.kernel_state


@pytest.mark.asyncio
async def test_composable_strategy_startup_recovery_ignores_pending_cancel_open_orders():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=None),
        strategy_id="kernel_test_pending_cancel_recover_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_pending_cancel_recover_001",
        extras={
            "startup_preflight_enabled": True,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._startup_block_reason = "broker_adapter_unavailable"
    strategy._startup_block_payload = {"reason": "broker_adapter_unavailable", "ok": False}

    broker = _BrokerStub()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="TQQQ", status="PendingCancel", order_ref="stkv1:test:pending-cancel"),
        ]
    )
    ctx.engine.broker_adapter = broker

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy._startup_block_reason is None
    assert "QQQ" in strategy.kernel_state


@pytest.mark.asyncio
async def test_composable_strategy_recovers_broker_live_position_startup_block_when_truth_flat():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=_BrokerStub()),
        strategy_id="kernel_test_broker_position_recover_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_broker_position_recover_001",
        extras={
            "startup_preflight_enabled": True,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._startup_block_reason = "broker_live_position_present"
    strategy._startup_block_payload = {"reason": "broker_live_position_present", "ok": False}

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy._startup_block_reason is None
    assert "QQQ" in strategy.kernel_state


@pytest.mark.asyncio
async def test_composable_strategy_startup_recovery_ignores_foreign_strategy_open_orders():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=None),
        strategy_id="kernel_test_foreign_order_recover_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_foreign_order_recover_001",
        extras={
            "startup_preflight_enabled": True,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._startup_block_reason = "broker_adapter_unavailable"
    strategy._startup_block_payload = {"reason": "broker_adapter_unavailable", "ok": False}

    broker = _BrokerStub()
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="TQQQ", status="Submitted", order_ref="stkv1:other_strategy:TQQQ:ENTRY:TRG:x"),
        ]
    )
    ctx.engine.broker_adapter = broker

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy._startup_block_reason is None
    assert "QQQ" in strategy.kernel_state


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_adopts_same_strategy_broker_position():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 70.0 if str(symbol).upper() == "SOXL" else 0.0)
    broker.open_orders = AsyncMock(return_value=[])
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="stocks_kernel_smh_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="stocks_kernel_smh_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._latest_symbol_snapshots["SOXL"] = MarketSnapshot(
        symbol="SOXL",
        ts_epoch_s=100.0,
        last_price=142.03,
        bid_price=142.02,
        ask_price=142.04,
    )
    note = {
        "decision_id": "decision-entry-1",
        "extras": {
            "signal_symbol": "SMH",
            "lane_id": "long_exec",
            "trigger_id": "trigger-1",
            "position_lot_id": "lot-soxl-1",
            "strategy_instance_id": "stocks_kernel_smh_restart_001",
            "protective_oco": {
                "symbol": "SOXL",
                "sl_offset_abs": 0.326786,
                "tp_offset_abs": 0.435714,
                "market_ref_px": 521.76,
                "quote_px": 521.76,
                "last_px": 521.76,
            },
        },
    }

    class _Pg:
        async def fetchrow(self, sql, *args):
            return {
                "strategy_id": strategy.strategy_id,
                "symbol": "SOXL",
                "pos_qty": 70.0,
                "avg_px": 142.03,
                "lot_id": "lot-soxl-1",
                "ledger_qty": 70.0,
                "lot_count": 1,
                "account_ids": ["DUO112752"],
                "order_id": "order-1",
                "broker_order_id": "398",
                "ib_perm_id": 1255568329,
                "order_ref": "stkv1:stocks_kernel_smh_001:SOXL:ENTRY:LOT:lot-soxl-1",
                "order_side": "BUY",
                "order_qty": 70.0,
                "limit_px": 142.03,
                "note": __import__("json").dumps(note),
            }

        async def fetchall(self, sql, *args):
            if "FROM core.core_position" in sql:
                return [{"strategy_id": strategy.strategy_id, "symbol": "SOXL", "pos_qty": 70.0}]
            return []

    ctx.pg = _Pg()
    audits: list[tuple[str, str, dict]] = []
    strategy._audit = lambda stage, symbol, reason, payload: audits.append((str(stage), str(reason), dict(payload or {})))

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is True
    assert preflight["adopted_symbols"] == ["SOXL"]
    lot = strategy.execution.ledger.get_lot("lot-soxl-1")
    assert lot is not None
    assert lot.symbol == "SOXL"
    assert lot.side == "LONG"
    assert lot.live_qty == pytest.approx(70.0)
    assert lot.avg_entry_price == pytest.approx(142.03)
    assert lot.extras["startup_adopted"] is True
    assert lot.extras["strategy_instance_id"] == "stocks_kernel_smh_restart_001"
    assert lot.extras["position_lot_id"] == "lot-soxl-1"
    assert lot.extras["position_owner_key"] == "stocks_kernel_smh_restart_001:SOXL:lot-soxl-1"
    assert lot.extras["protection_state"]["protection_lifecycle"] == "repair_pending"
    assert lot.extras["protective_oco"]["market_ref_px"] == pytest.approx(142.03)
    assert any(stage == "startup_broker_position_adopted" for stage, _, _ in audits)


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_rejects_broker_position_without_db_truth():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 70.0 if str(symbol).upper() == "SOXL" else 0.0)
    broker.open_orders = AsyncMock(return_value=[])
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="stocks_kernel_smh_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="stocks_kernel_smh_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    class _Pg:
        async def fetchrow(self, sql, *args):
            return None

        async def fetchall(self, sql, *args):
            return []

    ctx.pg = _Pg()

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is False
    assert preflight["reason"] == "broker_live_position_present"
    assert preflight["broker_checks"][0]["adoption"]["reason"] == "db_position_missing"


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_adopts_own_lane_with_sibling_broker_position():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 2.0 if str(symbol).upper() == "NVDA" else 0.0)
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="NVDA",
                status="Submitted",
                order_ref="sl:DU123|nvda_minute_flip_paper_001|NVDA|",
            ),
            SimpleNamespace(
                symbol="NVDA",
                status="Submitted",
                order_ref="sl:DU123|nvda_minute_flip_paper_002|NVDA|",
            ),
        ]
    )
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="nvda_minute_flip_paper_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA", "short_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    class _Pg:
        async def fetchrow(self, sql, *args):
            if "WITH ledger AS" in str(sql):
                return {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "pos_qty": 1.0,
                    "avg_px": 226.0,
                    "lot_id": "lot-nvda-1",
                    "ledger_qty": 1.0,
                    "lot_count": 1,
                    "account_ids": ["DU123"],
                    "order_id": "order-1",
                    "broker_order_id": "398",
                    "ib_perm_id": 1255568329,
                    "order_ref": "stkv1:nvda_minute_flip_paper_001:NVDA:ENTRY:LOT:lot-nvda-1",
                    "order_side": "BUY",
                    "order_qty": 1.0,
                    "limit_px": 226.0,
                    "note": __import__("json").dumps(
                        {"extras": {"account_id": "DU123", "strategy_instance_id": "nvda_minute_flip_paper_001"}}
                    ),
                }
            if "strategy_instance_id = $2 OR strategy_id = $2" in str(sql):
                return {"qty": 1.0}
            return {"qty": 2.0}

        async def fetchall(self, sql, *args):
            if "FROM core.core_position" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "pos_qty": 1.0},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "pos_qty": 1.0},
                ]
            if "FROM core.core_order" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "1"},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "2"},
                ]
            return []

    ctx.pg = _Pg()

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is True
    assert preflight["adopted_symbols"] == ["NVDA"]
    assert preflight["broker_checks"][0]["broker_account_qty"] == pytest.approx(2.0)
    assert preflight["broker_checks"][0]["broker_qty"] == pytest.approx(1.0)
    assert strategy.execution.ledger.get_lot("lot-nvda-1") is not None


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_ignores_foreign_owner_position():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 2.0 if str(symbol).upper() == "NVDA" else 0.0)
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="NVDA",
                status="Submitted",
                order_ref="sl:DU123|nvda_minute_flip_paper_001|NVDA|",
            ),
            SimpleNamespace(
                symbol="NVDA",
                status="Submitted",
                order_ref="sl:DU123|nvda_minute_flip_paper_002|NVDA|",
            ),
        ]
    )
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="agent_paper_bridge_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="agent_paper_bridge_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA", "short_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    class _Pg:
        async def fetchrow(self, sql, *args):
            if "strategy_instance_id = $2 OR strategy_id = $2" in str(sql):
                return {"qty": 0.0}
            return {"qty": 2.0}

        async def fetchall(self, sql, *args):
            if "FROM core.core_position" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "pos_qty": 1.0},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "pos_qty": 1.0},
                ]
            if "FROM core.core_order" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "1"},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "2"},
                ]
            return []

    ctx.pg = _Pg()

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is True
    assert preflight["broker_checks"][0]["reason"] == "foreign_owner_position_ignored"
    assert strategy.execution.ledger.list_lots(strategy_id=strategy.strategy_id, symbol="NVDA", include_closed=False) == []


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_inherits_legacy_open_order_owner_from_durable_row():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 2.0 if str(symbol).upper() == "NVDA" else 0.0)
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="aux:NVDA:legacy"),
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="sl:DU123|nvda_minute_flip_paper_001|NVDA|"),
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="sl:DU123|nvda_minute_flip_paper_002|NVDA|"),
        ]
    )
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="nvda_minute_flip_paper_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA", "short_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    class _Pg:
        async def fetchrow(self, sql, *args):
            sql_s = str(sql)
            if "SELECT strategy_id" in sql_s and "WHERE order_ref = $1" in sql_s:
                assert args[:2] == ("aux:NVDA:legacy", "NVDA")
                return {"strategy_id": "nvda_minute_flip_paper_001"}
            if "WITH ledger AS" in sql_s:
                return {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "pos_qty": 1.0,
                    "avg_px": 226.0,
                    "lot_id": "lot-nvda-legacy-1",
                    "ledger_qty": 1.0,
                    "lot_count": 1,
                    "account_ids": ["DU123"],
                    "order_id": "order-legacy-1",
                    "broker_order_id": "398",
                    "ib_perm_id": 1255568329,
                    "order_ref": "stkv1:nvda_minute_flip_paper_001:NVDA:ENTRY:LOT:lot-nvda-legacy-1",
                    "order_side": "BUY",
                    "order_qty": 1.0,
                    "limit_px": 226.0,
                    "note": __import__("json").dumps(
                        {"extras": {"account_id": "DU123", "strategy_instance_id": "nvda_minute_flip_paper_001"}}
                    ),
                }
            if "strategy_instance_id = $2 OR strategy_id = $2" in sql_s:
                return {"qty": 1.0}
            return {"qty": 2.0}

        async def fetchall(self, sql, *args):
            if "FROM core.core_position" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "pos_qty": 1.0},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "pos_qty": 1.0},
                ]
            if "FROM core.core_order" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "1"},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "2"},
                ]
            return []

    ctx.pg = _Pg()

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is True
    assert preflight["adopted_symbols"] == ["NVDA"]
    assert preflight["broker_checks"][0]["unknown_order_refs"] == []
    assert "aux:NVDA:legacy" in preflight["broker_checks"][0]["open_order_refs"]
    assert preflight["broker_checks"][0]["foreign_order_refs"] == ["sl:DU123|nvda_minute_flip_paper_002|NVDA|"]
    assert strategy.execution.ledger.get_lot("lot-nvda-legacy-1") is not None


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_treats_legacy_open_order_durable_other_owner_as_foreign():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 2.0 if str(symbol).upper() == "NVDA" else 0.0)
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="aux:NVDA:legacy"),
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="sl:DU123|nvda_minute_flip_paper_001|NVDA|"),
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="sl:DU123|nvda_minute_flip_paper_002|NVDA|"),
        ]
    )
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="agent_paper_bridge_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="agent_paper_bridge_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA", "short_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    class _Pg:
        async def fetchrow(self, sql, *args):
            sql_s = str(sql)
            if "SELECT strategy_id" in sql_s and "WHERE order_ref = $1" in sql_s:
                assert args[:2] == ("aux:NVDA:legacy", "NVDA")
                return {"strategy_id": "nvda_minute_flip_paper_001"}
            if "strategy_instance_id = $2 OR strategy_id = $2" in sql_s:
                return {"qty": 0.0}
            return {"qty": 2.0}

        async def fetchall(self, sql, *args):
            if "FROM core.core_position" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "pos_qty": 1.0},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "pos_qty": 1.0},
                ]
            if "FROM core.core_order" in str(sql):
                return [
                    {"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "1"},
                    {"strategy_id": "nvda_minute_flip_paper_002", "symbol": "NVDA", "side": "SELL", "qty": 1.0, "state": "SUBMITTED", "broker_order_id": "2"},
                ]
            return []

    ctx.pg = _Pg()

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is True
    assert preflight["broker_checks"][0]["reason"] == "foreign_owner_position_ignored"
    assert preflight["broker_checks"][0]["unknown_order_refs"] == []
    assert "aux:NVDA:legacy" in preflight["broker_checks"][0]["foreign_order_refs"]
    assert strategy.execution.ledger.list_lots(strategy_id=strategy.strategy_id, symbol="NVDA", include_closed=False) == []


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_keeps_unknown_legacy_open_order_fail_closed():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(side_effect=lambda symbol: 1.0 if str(symbol).upper() == "NVDA" else 0.0)
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(symbol="NVDA", status="Submitted", order_ref="aux:NVDA:unknown"),
        ]
    )
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="nvda_minute_flip_paper_001",
        price_cache=_PriceCacheStub(),
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA", "short_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    class _Pg:
        async def fetchrow(self, sql, *args):
            sql_s = str(sql)
            if "SELECT strategy_id" in sql_s and "WHERE order_ref = $1" in sql_s:
                return None
            if "WITH ledger AS" in sql_s:
                return {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "pos_qty": 1.0,
                    "avg_px": 226.0,
                    "lot_id": "lot-nvda-unknown-1",
                    "ledger_qty": 1.0,
                    "lot_count": 1,
                    "account_ids": ["DU123"],
                    "order_id": "order-unknown-1",
                    "broker_order_id": "398",
                    "ib_perm_id": 1255568329,
                    "order_ref": "stkv1:nvda_minute_flip_paper_001:NVDA:ENTRY:LOT:lot-nvda-unknown-1",
                    "order_side": "BUY",
                    "order_qty": 1.0,
                    "limit_px": 226.0,
                    "note": __import__("json").dumps(
                        {"extras": {"account_id": "DU123", "strategy_instance_id": "nvda_minute_flip_paper_001"}}
                    ),
                }
            if "strategy_instance_id = $2 OR strategy_id = $2" in sql_s:
                return {"qty": 1.0}
            return {"qty": 1.0}

        async def fetchall(self, sql, *args):
            return []

    ctx.pg = _Pg()

    preflight = await strategy._run_startup_preflight()

    assert preflight["ok"] is False
    assert preflight["reason"] == "broker_live_position_present"
    assert preflight["broker_checks"][0]["unknown_order_refs"] == ["aux:NVDA:unknown"]
    assert preflight["broker_checks"][0]["adoption"]["reason"] == "open_orders_present"


@pytest.mark.asyncio
async def test_composable_strategy_builds_protective_oco_from_policy_plans():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        strategy_id="kernel_test_protective_oco_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_protective_oco_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "execution_profile": {"protection_enabled": True},
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 2, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.5},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = strategy.policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)
    entry_plan = strategy.policy.build_entry_plan(signal=signal, feature_context=feature_context)
    strategy.kernel_state["QQQ"] = {
        "stop_plan": strategy.policy.build_stop_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
        "take_profit_plan": strategy.policy.build_take_profit_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
    }

    exec_snapshot = MarketSnapshot(symbol="TQQQ", ts_epoch_s=100.0, last_price=125.0, extras={})
    protective_oco = strategy._build_protective_oco_payload(signal_symbol="QQQ", exec_symbol="TQQQ", snapshot=exec_snapshot)

    assert protective_oco["symbol"] == "TQQQ"
    assert protective_oco["sl_offset_abs"] == pytest.approx(1.125)
    assert protective_oco["tp_offset_abs"] == pytest.approx(1.5)
    assert protective_oco["market_ref_px"] == pytest.approx(125.0)
    assert protective_oco["last_px"] == pytest.approx(125.0)
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_uses_exec_symbol_snapshot_for_protection_market_ref():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        strategy_id="kernel_test_protective_exec_ref_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_protective_exec_ref_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "execution_profile": {"protection_enabled": True},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    feature_context = FeatureContext(
        symbol="SMH",
        snapshot=MarketSnapshot(symbol="SMH", ts_epoch_s=100.0, last_price=521.76, extras={"planned_qty": 70, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="SMH"),
        values={"v3m": 0.02, "atr_eff": 0.43571428571428733},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = strategy.policy.build_signal(symbol="SMH", feature_context=feature_context, gate_report=gate_report)
    entry_plan = strategy.policy.build_entry_plan(signal=signal, feature_context=feature_context)
    strategy.kernel_state["SMH"] = {
        "stop_plan": strategy.policy.build_stop_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
        "take_profit_plan": strategy.policy.build_take_profit_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
    }
    strategy._latest_symbol_snapshots["SOXL"] = MarketSnapshot(symbol="SOXL", ts_epoch_s=100.0, last_price=142.03, extras={})

    protective_oco = strategy._build_protective_oco_payload(
        signal_symbol="SMH",
        exec_symbol="SOXL",
        snapshot=feature_context.snapshot,
    )

    assert protective_oco["symbol"] == "SOXL"
    assert protective_oco["market_ref_px"] == pytest.approx(142.03)
    assert protective_oco["last_px"] == pytest.approx(142.03)


@pytest.mark.asyncio
async def test_composable_strategy_runs_safety_invariants_while_system_paused_for_exec_symbol():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        strategy_id="kernel_test_paused_safety_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_paused_safety_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "execution_profile": {"protection_enabled": True},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._system_paused = True
    strategy.execution = SimpleNamespace()
    strategy._check_runtime_safety_invariants = AsyncMock()

    await strategy.on_features(
        "SOXL",
        {
            "ts_epoch_s": 1777994460.0,
            "last_price": 142.03,
            "bid_price": 142.02,
            "ask_price": 142.04,
            "session": "RTH",
        },
    )

    strategy._check_runtime_safety_invariants.assert_awaited_once()
    kwargs = strategy._check_runtime_safety_invariants.await_args.kwargs
    assert kwargs["signal_symbol"] == "SMH"
    assert kwargs["snapshot"].symbol == "SOXL"
    assert strategy._latest_snapshot_age_sec(symbol="SOXL") is not None
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_releases_recoverable_kill_switch_while_system_paused():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        strategy_id="kernel_test_paused_kill_release_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_paused_kill_release_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window"],
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._system_paused = True
    strategy._symbol_kill_switch["SOXL"] = "broker_truth_mismatch"
    strategy._binding_broker_account_isolation = AsyncMock(
        return_value={"blocked": False, "broker_qty": 0.0, "open_order_count": 0, "open_order_refs": []}
    )

    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 1777994460.0,
            "last_price": 142.03,
            "bid_price": 142.02,
            "ask_price": 142.04,
            "session": "RTH",
        },
    )

    assert "SOXL" not in strategy._symbol_kill_switch
    strategy._binding_broker_account_isolation.assert_any_await(
        signal_symbol="SMH",
        exec_symbol="SOXL",
    )
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_system_pause_latch_releases_when_production_state_go(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "account_id": "DUO112752",
            "strategy_instance_id": "nvda_minute_flip_paper_001",
            "asset_type": "STK",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._system_paused = True
    artifacts = {
        "runtime/readiness/production_state/production_state_latest.json": {
            "go_no_go": "go",
            "entry_go_no_go": "partial",
            "safety_go_no_go": "go",
            "global_halt": False,
            "failures": [
                {
                    "issue_key": "system_health:degraded",
                    "service": "SYSTEM_HEALTH",
                    "error_type": "system_health_not_running",
                    "detail": "degraded",
                    "reason": "runtime_reconciliation_anomaly_detected",
                }
            ],
        }
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    decision = strategy._entry_tradability_decision_while_paused(symbol="NVDA", session="POST")

    assert decision["allow"] is True
    assert decision["authority"] == "TradabilityService"
    assert decision["reason"] == "system_pause_latch_released_by_production_state"
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_system_pause_latch_keeps_hard_global_blocker(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "account_id": "DUO112752",
            "strategy_instance_id": "nvda_minute_flip_paper_001",
            "asset_type": "STK",
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._system_paused = True
    artifacts = {
        "runtime/readiness/production_state/production_state_latest.json": {
            "go_no_go": "no_go",
            "safety_go_no_go": "no_go",
            "global_halt": True,
            "failures": [
                {
                    "issue_key": "broker_truth_missing",
                    "service": "BROKER_TRUTH",
                    "error_type": "broker_truth_missing",
                    "detail": "broker_truth_missing",
                }
            ],
        }
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    decision = strategy._entry_tradability_decision_while_paused(symbol="NVDA", session="POST")

    assert decision["allow"] is False
    assert decision["halt_class"] == "GLOBAL_HALT"
    assert "safety_kernel_global_halt" in decision["blockers"]
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_does_not_publish_latched_system_pause_when_state_go(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={"universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}]},
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._system_paused = True
    artifacts = {
        "runtime/readiness/production_state/production_state_latest.json": {
            "go_no_go": "go",
            "safety_go_no_go": "go",
            "global_halt": False,
            "failures": [{"issue_key": "system_health:degraded", "detail": "degraded"}],
        }
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    strategy._emit_strategy_tradability_heartbeat(symbol="NVDA", session="POST", reason="test")

    heartbeat_path = tmp_path / "runtime/readiness/strategy_tradability/heartbeats/nvda_minute_flip_paper_001.json"
    assert heartbeat_path.exists()
    heartbeat = json.loads(heartbeat_path.read_text())
    assert "system_paused" not in heartbeat["blockers"]
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_blocks_protective_oco_when_lane_authority_reserved():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_protective_oco_reserved_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_protective_oco_reserved_001",
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "execution_profile": {"protection_enabled": True},
            "universe": [{
                "signal_symbol": "QQQ",
                "execution_lanes": [
                    {"lane_id": "long_lane", "symbol": "TQQQ", "side": "LONG", "protection_authority": "portfolio"},
                    {"lane_id": "short_lane", "symbol": "SQQQ", "side": "SHORT"},
                ],
            }],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, extras={"planned_qty": 2, "trigger_id": "t1"}),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": 0.02, "atr_eff": 1.5},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = strategy.policy.build_signal(symbol="QQQ", feature_context=feature_context, gate_report=gate_report)
    entry_plan = strategy.policy.build_entry_plan(signal=signal, feature_context=feature_context)
    strategy.kernel_state["QQQ"] = {
        "stop_plan": strategy.policy.build_stop_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
        "take_profit_plan": strategy.policy.build_take_profit_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
    }

    protective_oco = strategy._build_protective_oco_payload(
        signal_symbol="QQQ",
        exec_symbol="TQQQ",
        lane_id="long_lane",
    )

    assert protective_oco == {}


@pytest.mark.asyncio
async def test_composable_strategy_builds_overnight_execution_session_extras():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_overnight_exec_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_overnight_exec_001",
        extras={
            "execution_profile": {"protection_enabled": True},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    snapshot = MarketSnapshot(symbol="SMH", ts_epoch_s=100.0, last_price=40.0, session="OVERNIGHT")

    extras = strategy._execution_session_extras(snapshot=snapshot)

    assert extras == {"outside_rth": True, "exchange_override": "OVERNIGHT"}


@pytest.mark.asyncio
async def test_composable_strategy_builds_overnight_protective_oco_payload_using_entry_exchange_by_default():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_overnight_oco_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_overnight_oco_001",
        extras={
            "execution_profile": {"protection_enabled": True},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    feature_context = FeatureContext(
        symbol="SMH",
        snapshot=MarketSnapshot(symbol="SMH", ts_epoch_s=100.0, last_price=250.0, session="OVERNIGHT"),
        history=HistoryBundle(symbol="SMH"),
        values={"v3m": 0.02, "atr_eff": 1.5},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = strategy.policy.build_signal(symbol="SMH", feature_context=feature_context, gate_report=gate_report)
    entry_plan = strategy.policy.build_entry_plan(signal=signal, feature_context=feature_context)
    strategy.kernel_state["SMH"] = {
        "stop_plan": strategy.policy.build_stop_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
        "take_profit_plan": strategy.policy.build_take_profit_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
    }

    protective_oco = strategy._build_protective_oco_payload(
        signal_symbol="SMH",
        exec_symbol="SOXL",
        snapshot=feature_context.snapshot,
    )

    assert protective_oco["outside_rth"] is True
    assert protective_oco["exchange_override"] == "OVERNIGHT"
    assert protective_oco["sl_order_type"] == "STP"
    assert "sl_limit_offset_abs" not in protective_oco


@pytest.mark.asyncio
async def test_composable_strategy_can_override_overnight_protective_exchange():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_overnight_oco_exchange_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_overnight_oco_exchange_001",
        extras={
            "execution_profile": {
                "protection_enabled": True,
                "protection_overnight_exchange": "OVERNIGHT",
                "protection_offhours_stop_order_type": "STP_LMT",
            },
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    feature_context = FeatureContext(
        symbol="SMH",
        snapshot=MarketSnapshot(symbol="SMH", ts_epoch_s=100.0, last_price=250.0, session="OVERNIGHT"),
        history=HistoryBundle(symbol="SMH"),
        values={"v3m": 0.02, "atr_eff": 1.5},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )
    gate_report = GateEvaluationReport(allowed=True, decisions=())
    signal = strategy.policy.build_signal(symbol="SMH", feature_context=feature_context, gate_report=gate_report)
    entry_plan = strategy.policy.build_entry_plan(signal=signal, feature_context=feature_context)
    strategy.kernel_state["SMH"] = {
        "stop_plan": strategy.policy.build_stop_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
        "take_profit_plan": strategy.policy.build_take_profit_plan(signal=signal, entry_plan=entry_plan, feature_context=feature_context),
    }

    protective_oco = strategy._build_protective_oco_payload(
        signal_symbol="SMH",
        exec_symbol="SOXL",
        snapshot=feature_context.snapshot,
    )

    assert protective_oco["outside_rth"] is True
    assert protective_oco["exchange_override"] == "OVERNIGHT"
    assert protective_oco["sl_order_type"] == "STP_LMT"
    assert protective_oco["sl_limit_offset_abs"] > 0.0


@pytest.mark.asyncio
async def test_composable_strategy_arms_entry_rearm_cooldown_from_terminal_entry_update():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_rearm_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_rearm_001",
        extras={
            "order_namespace": "stkv1",
            "entry_rearm_sec": 30,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    submit = strategy.execution.open_entry_lot(
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )

    await strategy.on_order_update(
        SimpleNamespace(
            order_ref=submit.order_ref,
            symbol="TQQQ",
            broker_order_id="11",
            ib_perm_id=123,
            state="CANCELLED",
            order_type="LMT",
        )
    )

    assert strategy._entry_rearm_block_until["TQQQ"] > datetime.now(ET).timestamp()


@pytest.mark.asyncio
async def test_composable_strategy_stale_pending_entry_cancels_and_arms_timeout_cooldown():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        order_ref="oref-stale-1",
                        symbol="TQQQ",
                        broker_order_id="77",
                        state="Submitted",
                    )
                ]
            )

        async def cancel_order(self, order_id):
            return {"ok": True, "order_id": order_id}

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_stale_pending_timeout_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_stale_pending_timeout_001",
        extras={
            "order_namespace": "stkv1",
            "entry_timeout_reentry_cooldown_sec": 75,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._resolve_broker_cancel_fn = lambda: adapter.cancel_order
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="stale-lot-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d1",
            opened_by_trigger_id="t1",
            entry_order_ref="oref-stale-1",
            live_qty=0.0,
            filled_qty=0.0,
            avg_entry_price=None,
            state="pending_entry",
            extras={},
        )
    )
    strategy._broker_account_isolation = AsyncMock(return_value={"blocked": False})
    strategy.execution_semantics_module = SimpleNamespace(
        entry_pending_timeout_sec=lambda: 60.0,
        pending_entry_quarantine_candidate=lambda **kwargs: (
            True,
            {"age_sec": 65.0, "timeout_sec": 60.0},
        )
    )

    await strategy._reap_stale_pending_entries(
        instrument=strategy.config.instruments[0],
        now_ts_epoch_s=100.0,
    )

    lot = strategy.execution.ledger.get_lot("stale-lot-1")
    assert lot is not None
    assert lot.state == "quarantined"
    adapter.open_orders.assert_awaited_once_with(force_refresh=False)
    assert strategy._entry_rearm_block_until["TQQQ"] == pytest.approx(175.0)


@pytest.mark.asyncio
async def test_composable_strategy_rejects_force_test_entry_config():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_force_entry_rejected_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_force_entry_rejected_001",
        extras={
            "force_test_entry_side": "SHORT",
            "force_test_entry_trigger_id": "probe",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )

    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)

    with pytest.raises(ValueError, match="force_test_entry config is not supported"):
        await strategy.initialize()


@pytest.mark.asyncio
async def test_composable_strategy_blocks_new_entry_after_cutoff():
    submitted = {"count": 0}

    async def _submit_intent(_envelope):
        submitted["count"] += 1
        return {"ok": True}

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_cutoff_001",
        submit_intent=_submit_intent,
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_cutoff_001",
        capital_limit_usd=10_000,
        extras={
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"hard_stop_atr_mult": 0.75},
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "no_new_after_et": "15:45",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    ts_epoch_s = datetime(2026, 4, 1, 15, 50, tzinfo=ET).timestamp()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": ts_epoch_s,
            "now_epoch_s": ts_epoch_s + 1,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy.kernel_state["QQQ"]["signal"].action == "enter"
    assert submitted["count"] == 0


@pytest.mark.asyncio
async def test_composable_strategy_exec_snapshot_ready_from_price_cache():
    now_ts = datetime.now(ET).timestamp()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(
            _strategy_engine=SimpleNamespace(
                price_cache=_PriceCacheStub(
                    snapshot=SimpleNamespace(last=48.25, bid=48.2, ask=48.3, ts_us=int(now_ts * 1_000_000)),
                    minute_stats=SimpleNamespace(vwap=48.1),
                )
            )
        ),
        strategy_id="kernel_test_exec_snapshot_cache_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_exec_snapshot_cache_001",
        capital_limit_usd=10_000,
        extras={"data_profile": "equity_offhours_standard"},
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._exec_symbol_ready_for_entry(exec_symbol="SOXS") is True


@pytest.mark.asyncio
async def test_composable_strategy_trade_budget_uses_price_cache_exec_snapshot():
    now_ts = datetime.now(ET).timestamp()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(
            _strategy_engine=SimpleNamespace(
                price_cache=_PriceCacheStub(
                    snapshot=SimpleNamespace(last=40.0, bid=39.9, ask=40.1, ts_us=int(now_ts * 1_000_000)),
                    minute_stats=SimpleNamespace(vwap=39.8),
                )
            )
        ),
        strategy_id="kernel_test_exec_budget_cache_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_exec_budget_cache_001",
        capital_limit_usd=10_000,
        extras={"data_profile": "equity_offhours_standard"},
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    adjusted = strategy._maybe_apply_trade_budget(
        entry_plan=EntryPlan(symbol="SOXS", side="SHORT", qty=1.0),
        signal_symbol="SMH",
    )

    assert adjusted.qty == 250.0


@pytest.mark.asyncio
async def test_composable_strategy_resolves_exec_snapshot_from_latest_feature_cache():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_latest_exec_snapshot_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_latest_exec_snapshot_001",
        extras={"data_profile": "equity_offhours_standard"},
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    strategy._remember_latest_snapshot(
        symbol="SOXS",
        features={
            "ts_epoch_s": 100.0,
            "last_price": 39.25,
            "bid_price": 39.24,
            "ask_price": 39.26,
            "session": "OVERNIGHT",
        },
    )

    snapshot = strategy._resolve_exec_snapshot(exec_symbol="SOXS")

    assert snapshot is not None
    assert snapshot.last_price == pytest.approx(39.25)


@pytest.mark.asyncio
async def test_composable_strategy_waits_briefly_for_exec_snapshot_before_submit():
    submitted = {"count": 0, "symbols": []}

    async def _submit_intent(_envelope):
        submitted["count"] += 1
        submitted["symbols"].append(_envelope.symbol)
        return {"order_id": 1, "broker_order_id": "1", "perm_id": 123}

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_exec_snapshot_wait_001",
        submit_intent=_submit_intent,
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_exec_snapshot_wait_001",
        capital_limit_usd=10_000,
        ttl_ms=2_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "policy_params": {"rule_engine_parity": False},
            "enforce_same_day_flatten": False,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": [],
            "require_exec_symbol_snapshot": True,
            "exec_snapshot_wait_ms": 120,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    async def _publish_exec_snapshot():
        await asyncio.sleep(0.02)
        strategy._remember_latest_snapshot(
            symbol="TQQQ",
            features={
                "ts_epoch_s": 100.02,
                "last_price": 40.0,
                "bid_price": 39.99,
                "ask_price": 40.01,
                "session": "RTH",
            },
        )

    task = asyncio.create_task(_publish_exec_snapshot())
    try:
        await strategy.on_features(
            "QQQ",
            {
                "ts_epoch_s": 100.0,
                "now_epoch_s": 101.0,
                "last_price": 500.0,
                "session": "RTH",
                "vwap": 499.0,
                "atr_eff": 1.0,
                "v3m": 0.02,
                "trigger_id": "t1",
                "bars_1m": [
                    HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                    for i in range(1, 16)
                ],
            },
        )
    finally:
        await task

    assert submitted["count"] == 1
    assert submitted["symbols"] == ["TQQQ"]


@pytest.mark.asyncio
async def test_composable_strategy_releases_recoverable_symbol_kill_switch_once_broker_is_clean():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="kernel_test_kill_switch_release_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_kill_switch_release_001",
        extras={
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["SQQQ"] = "broker_binding_open_orders_present"

    released = await strategy._maybe_release_symbol_kill_switch(
        signal_symbol="QQQ",
        exec_symbol="SQQQ",
    )

    assert released is True
    assert "SQQQ" not in strategy._symbol_kill_switch


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_releases_stale_unsafe_position_kill_switch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_002",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_002",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["NVDA"] = "unsafe_position_without_protection"
    artifacts = {
        "runtime/readiness/position_safety/position_safety_latest.json": {
            "go_no_go": "go",
            "active_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "records": [],
            "generated_at": "2026-05-13T02:03:33-04:00",
        },
        "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json": {
            "go_no_go": "go",
            "positions": [],
            "open_orders": [],
            "generated_at": "2026-05-13T02:03:33-04:00",
        },
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    strategy._emit_strategy_tradability_heartbeat(symbol="NVDA", reason="test")

    assert "NVDA" not in strategy._symbol_kill_switch
    heartbeat_path = tmp_path / "runtime/readiness/strategy_tradability/heartbeats/nvda_minute_flip_paper_002.json"
    assert heartbeat_path.exists()
    assert "unsafe_position_without_protection" not in heartbeat_path.read_text()
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_uses_newer_broker_truth_flat_summary(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["NVDA"] = "live_position_missing_protection"
    artifacts = {
        "runtime/readiness/position_safety/position_safety_latest.json": {
            "go_no_go": "go",
            "active_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "records": [],
            "generated_at": "2026-05-13T06:25:41-04:00",
        },
        "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json": {
            "go_no_go": "go",
            "positions": [{"symbol": "NVDA", "qty": 1.0}],
            "open_orders": [{"symbol": "NVDA", "order_id": 1234}],
            "generated_at": "2026-05-13T06:25:41-04:00",
        },
        "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json": {
            "go_no_go": "go",
            "blockers": [],
            "active_quarantines": [],
            "live_broker_open_order_count": 0,
            "live_broker_position_count": 0,
            "open_core_order_count": 0,
            "nonzero_core_position_count": 0,
            "nonzero_ledger_position_count": 0,
            "projection_divergence_count": 0,
            "dirty_lot_position_count": 0,
            "order_truth_active_quarantine_count": 0,
            "generated_at": "2026-05-13T06:25:58-04:00",
        },
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    strategy._emit_strategy_tradability_heartbeat(symbol="NVDA", reason="test")

    assert "NVDA" not in strategy._symbol_kill_switch
    heartbeat_path = tmp_path / "runtime/readiness/strategy_tradability/heartbeats/nvda_minute_flip_paper_001.json"
    assert heartbeat_path.exists()
    assert "live_position_missing_protection" not in heartbeat_path.read_text()
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_releases_missing_protection_when_position_safety_protected(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["NVDA"] = "live_position_missing_protection"
    artifacts = {
        "runtime/readiness/position_safety/position_safety_latest.json": {
            "go_no_go": "go",
            "active_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "records": [
                {
                    "symbol": "NVDA",
                    "protected_or_flattening": True,
                    "contract_strategy_ids": ["nvda_minute_flip_paper_001"],
                }
            ],
            "generated_at": "2026-05-13T06:42:15-04:00",
        },
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    strategy._emit_strategy_tradability_heartbeat(symbol="NVDA", reason="test")

    assert "NVDA" not in strategy._symbol_kill_switch
    heartbeat_path = tmp_path / "runtime/readiness/strategy_tradability/heartbeats/nvda_minute_flip_paper_001.json"
    assert heartbeat_path.exists()
    assert "live_position_missing_protection" not in heartbeat_path.read_text()
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_releases_stale_flatten_timeout_kill_switch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["NVDA"] = "flatten_not_converged_timeout"
    artifacts = {
        "runtime/readiness/position_safety/position_safety_latest.json": {
            "go_no_go": "go",
            "active_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "records": [],
            "generated_at": "2026-05-13T02:22:08-04:00",
        },
        "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json": {
            "go_no_go": "go",
            "positions": [],
            "open_orders": [],
            "generated_at": "2026-05-13T02:22:08-04:00",
        },
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    strategy._emit_strategy_tradability_heartbeat(symbol="NVDA", reason="test")

    assert "NVDA" not in strategy._symbol_kill_switch
    heartbeat_path = tmp_path / "runtime/readiness/strategy_tradability/heartbeats/nvda_minute_flip_paper_001.json"
    assert heartbeat_path.exists()
    assert "flatten_not_converged_timeout" not in heartbeat_path.read_text()
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_recovers_restart_startup_block_before_reporting():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._startup_block_reason = "broker_live_position_present"
    strategy._startup_block_payload = {"reason": "broker_live_position_present"}

    async def _recover():
        strategy._startup_block_reason = None
        strategy._startup_block_payload = {}
        return True

    strategy._maybe_recover_startup_block = AsyncMock(side_effect=_recover)

    recovered = await strategy._recover_startup_block_for_strategy_tradability()

    assert recovered is True
    assert strategy._startup_block_reason is None
    assert strategy._startup_block_payload == {}
    strategy._maybe_recover_startup_block.assert_awaited_once()
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_releases_broker_truth_kill_switch_when_position_is_verified_protected(monkeypatch):
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="stocks_kernel_smh_001",
    )
    params = SimpleNamespace(
        strategy_id="stocks_kernel_smh_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["SOXL"] = "broker_truth_mismatch"
    strategy._binding_broker_account_isolation = AsyncMock(
        return_value={
            "blocked": True,
            "reason": "broker_binding_live_position_present",
            "broker_qty": 53.0,
            "open_order_count": 2,
            "open_order_refs": ["sl:DUO112752|stocks_kernel_smh_001|SOXL|abc", "tp:DUO112752|stocks_kernel_smh_001|SOXL|abc"],
        }
    )
    artifacts = {
        "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json": {
            "go_no_go": "go",
            "blockers": [],
            "active_quarantines": [],
            "projection_divergence_count": 0,
            "dirty_lot_position_count": 0,
            "generated_at": "2026-05-11T13:25:26-04:00",
        },
        "runtime/readiness/position_safety/position_safety_latest.json": {
            "go_no_go": "go",
            "active_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "generated_at": "2026-05-11T13:25:25-04:00",
            "records": [
                {
                    "symbol": "SOXL",
                    "candidate_strategy_ids": ["stocks_kernel_smh_001"],
                    "protected_or_flattening": True,
                }
            ],
        },
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    released = await strategy._maybe_release_symbol_kill_switch(
        signal_symbol="SMH",
        exec_symbol="SOXL",
    )

    assert released is True
    assert "SOXL" not in strategy._symbol_kill_switch
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_recovery_broker_account_isolation_classifies_protection_refs_by_owner():
    broker = _BrokerStub()
    broker.position_qty = AsyncMock(return_value=1.0)
    broker.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="NVDA",
                order_ref="sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                status="PreSubmitted",
            ),
            SimpleNamespace(
                symbol="NVDA",
                order_ref="tp:DUO112752|other_strategy|NVDA|",
                status="PreSubmitted",
            ),
            SimpleNamespace(
                symbol="NVDA",
                order_ref="sl:DUO112752|nvda_minute_flip_paper_001|TSLA|",
                status="PreSubmitted",
            ),
        ]
    )
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="nvda_minute_flip_paper_001",
    )
    params = SimpleNamespace(
        strategy_id="nvda_minute_flip_paper_001",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    isolation = await strategy.recovery_module.broker_account_isolation(symbol="NVDA")

    assert isolation["blocked"] is True
    assert isolation["open_order_refs"] == ["sl:DUO112752|nvda_minute_flip_paper_001|NVDA|", "sl:DUO112752|nvda_minute_flip_paper_001|TSLA|"]
    assert isolation["foreign_order_refs"] == ["tp:DUO112752|other_strategy|NVDA|"]
    assert isolation["unknown_order_refs"] == ["sl:DUO112752|nvda_minute_flip_paper_001|TSLA|"]
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_keeps_broker_truth_kill_switch_when_position_safety_not_go(monkeypatch):
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="stocks_kernel_smh_001",
    )
    params = SimpleNamespace(
        strategy_id="stocks_kernel_smh_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["SOXL"] = "broker_truth_mismatch"
    strategy._binding_broker_account_isolation = AsyncMock(
        return_value={"blocked": True, "reason": "broker_binding_live_position_present", "broker_qty": 53.0}
    )
    artifacts = {
        "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json": {
            "go_no_go": "go",
            "blockers": [],
            "active_quarantines": [],
            "projection_divergence_count": 0,
            "dirty_lot_position_count": 0,
        },
        "runtime/readiness/position_safety/position_safety_latest.json": {
            "go_no_go": "no_go",
            "active_manual_lock_count": 0,
            "active_quarantine_count": 0,
            "records": [],
        },
    }
    monkeypatch.setattr(strategy, "_read_runtime_json", lambda path: dict(artifacts.get(path, {})))

    released = await strategy._maybe_release_symbol_kill_switch(
        signal_symbol="SMH",
        exec_symbol="SOXL",
    )

    assert released is False
    assert strategy._symbol_kill_switch["SOXL"] == "broker_truth_mismatch"
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_releases_missing_protection_kill_switch_once_broker_is_clean():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="kernel_test_missing_protection_release_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_missing_protection_release_001",
        extras={
            "universe": [{"signal_symbol": "NVDA", "long_exec_symbol": "NVDA"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["NVDA"] = "live_position_missing_protection"

    released = await strategy._maybe_release_symbol_kill_switch(signal_symbol="NVDA", exec_symbol="NVDA")

    assert released is True
    assert "NVDA" not in strategy._symbol_kill_switch


@pytest.mark.asyncio
async def test_composable_strategy_releases_reservation_failed_symbol_kill_switch_once_ready():
    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        system_health=SystemHealthController(),
        strategy_id="kernel_test_kill_switch_hold_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_kill_switch_hold_001",
        extras={
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["SQQQ"] = "reservation_failed"

    released = await strategy._maybe_release_symbol_kill_switch(
        signal_symbol="QQQ",
        exec_symbol="SQQQ",
    )

    assert released is True
    assert "SQQQ" not in strategy._symbol_kill_switch
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_reconciles_order_lifecycle_terminal_core_order():
    class _Pg:
        async def fetch(self, sql, *args):
            if "FROM core.core_order_event" in sql:
                return []
            assert "FROM core.core_order" in sql
            assert " decision_id" not in sql
            assert len(args) == 5
            return [
                {
                    "order_ref": "stkv1:alpha:SOXL:ENTRY:truncated",
                    "symbol": "SOXL",
                    "state": "FILLED",
                    "broker_order_id": "123",
                    "ib_perm_id": 456,
                }
            ]

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_order_lifecycle_db_terminal_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_order_lifecycle_db_terminal_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.recovery_module.resolve_pg = lambda: _Pg()
    strategy.order_lifecycle_module.register_created(
        order_ref="stkv1:alpha:SOXL:ENTRY:lot-1",
        symbol="SOXL",
        position_lot_id="lot-1",
        now_ts_epoch_s=100.0,
    )
    strategy.order_lifecycle_module.mark_accepted(
        order_ref="stkv1:alpha:SOXL:ENTRY:lot-1",
        now_ts_epoch_s=101.0,
        broker_order_id="123",
        perm_id=456,
    )
    strategy._symbol_kill_switch["SOXL"] = "ack_not_converged_timeout"

    await strategy._reconcile_order_lifecycle_from_db(exec_symbols=["SOXL"], now_ts_epoch_s=200.0)

    pending_count, pending_refs = strategy.order_lifecycle_module.pending_non_terminal_orders(symbols=["SOXL"])
    assert pending_count == 0
    assert pending_refs == ()
    assert "SOXL" not in strategy._symbol_kill_switch
    assert strategy.order_lifecycle_module.freeze_reason(symbol="SOXL") is None


@pytest.mark.asyncio
async def test_composable_strategy_reconciles_order_lifecycle_terminal_event_fallback():
    class _Pg:
        async def fetch(self, sql, *args):
            if "FROM core.core_order_event" in sql:
                return [
                    {
                        "order_ref": "",
                        "symbol": "SOXS",
                        "event_type": "order_cancelled",
                        "broker_order_id": "124",
                        "ib_perm_id": 457,
                    }
                ]
            assert "FROM core.core_order" in sql
            assert len(args) == 5
            return []

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_order_lifecycle_event_terminal_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_order_lifecycle_event_terminal_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.recovery_module.resolve_pg = lambda: _Pg()
    strategy.order_lifecycle_module.register_created(
        order_ref="stkv1:alpha:SOXS:FLATTEN:lot-2",
        symbol="SOXS",
        position_lot_id="lot-2",
        now_ts_epoch_s=100.0,
    )
    strategy.order_lifecycle_module.mark_accepted(
        order_ref="stkv1:alpha:SOXS:FLATTEN:lot-2",
        now_ts_epoch_s=101.0,
        broker_order_id="124",
        perm_id=457,
    )
    strategy._symbol_kill_switch["SOXS"] = "ack_not_converged_timeout"

    await strategy._reconcile_order_lifecycle_from_db(exec_symbols=["SOXS"], now_ts_epoch_s=200.0)

    pending_count, pending_refs = strategy.order_lifecycle_module.pending_non_terminal_orders(symbols=["SOXS"])
    assert pending_count == 0
    assert pending_refs == ()
    assert "SOXS" not in strategy._symbol_kill_switch
    assert strategy.order_lifecycle_module.freeze_reason(symbol="SOXS") is None


@pytest.mark.asyncio
async def test_composable_strategy_reconciles_order_lifecycle_by_event_decision_id_when_order_ref_truncated():
    decision_id = "stocks_kernel_smh_001:SOXL:1777998990000000:long:act-entry:lane-long_exec:qty-68"
    full_ref = (
        "stkv1:stocks_kernel_smh_001:SOXL:ENTRY:LANE:long_exec:"
        "TRG:stocks_kernel_smh_001_SOXL_1777998990000000_long_act-entry_lane-long_exec_qty-68:"
        "LOT:b3f052e88e6647bdb9ccf855877aa654"
    )

    class _Pg:
        async def fetch(self, sql, *args):
            if "FROM core.core_order_event" in sql:
                assert decision_id in set(args[5])
                return [
                    {
                        "order_ref": "",
                        "symbol": "SOXL",
                        "event_type": "ORDER_FILL",
                        "broker_order_id": "",
                        "ib_perm_id": None,
                        "decision_id": decision_id,
                    }
                ]
            assert "FROM core.core_order" in sql
            assert " decision_id" not in sql
            assert len(args) == 5
            return []

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="stocks_kernel_smh_001",
    )
    params = SimpleNamespace(
        strategy_id="stocks_kernel_smh_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.recovery_module.resolve_pg = lambda: _Pg()
    strategy.order_lifecycle_module.register_created(
        order_ref=full_ref,
        symbol="SOXL",
        position_lot_id="b3f052e88e6647bdb9ccf855877aa654",
        now_ts_epoch_s=100.0,
    )
    strategy.order_lifecycle_module.mark_accepted(order_ref=full_ref, now_ts_epoch_s=101.0)
    strategy._symbol_kill_switch["SOXL"] = "ack_not_converged_timeout"

    await strategy._reconcile_order_lifecycle_from_db(exec_symbols=["SOXL"], now_ts_epoch_s=200.0)

    pending_count, pending_refs = strategy.order_lifecycle_module.pending_non_terminal_orders(symbols=["SOXL"])
    assert pending_count == 0
    assert pending_refs == ()
    assert "SOXL" not in strategy._symbol_kill_switch
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_heartbeat_reconciles_order_lifecycle_before_reporting():
    class _Pg:
        async def fetch(self, sql, *args):
            if "FROM core.core_order_event" in sql:
                return []
            assert "FROM core.core_order" in sql
            return [
                {
                    "order_ref": "stkv1:alpha:SOXL:FLATTEN:lot-3",
                    "symbol": "SOXL",
                    "state": "FILLED",
                    "broker_order_id": "125",
                    "ib_perm_id": 458,
                }
            ]

    broker = _BrokerStub()
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=broker),
        strategy_id="kernel_test_order_lifecycle_heartbeat_terminal_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_order_lifecycle_heartbeat_terminal_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.recovery_module.resolve_pg = lambda: _Pg()
    strategy.order_lifecycle_module.register_created(
        order_ref="stkv1:alpha:SOXL:FLATTEN:lot-3",
        symbol="SOXL",
        position_lot_id="lot-3",
        now_ts_epoch_s=100.0,
    )
    strategy.order_lifecycle_module.mark_accepted(
        order_ref="stkv1:alpha:SOXL:FLATTEN:lot-3",
        now_ts_epoch_s=101.0,
        broker_order_id="125",
        perm_id=458,
    )

    await strategy._reconcile_heartbeat_order_lifecycle(symbol="SMH")

    pending_count, pending_refs = strategy.order_lifecycle_module.pending_non_terminal_orders(symbols=["SOXL"])
    assert pending_count == 0
    assert pending_refs == ()
    await strategy.on_shutdown("test_done")


def test_composable_strategy_extracts_broker_identity_from_pipeline_result_metadata():
    result = SimpleNamespace(
        metadata={
            "execution_result": {
                "order_id": 438,
                "metadata": {"perm_id": 1255569761},
            }
        }
    )

    broker_order_id, perm_id = ComposableStocksKernelStrategy._broker_identity_from_submit_result(result)

    assert broker_order_id == "438"
    assert perm_id == 1255569761


@pytest.mark.asyncio
async def test_composable_strategy_starts_tradability_heartbeat_for_production_blocking_without_health():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=_BrokerStub()),
        strategy_id="kernel_test_prod_tradability_without_health_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_prod_tradability_without_health_001",
        extras={
            "production_blocking": True,
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._tradability_heartbeat_task is not None
    assert strategy._tradability_heartbeat_task.done() is False

    await strategy.on_shutdown("test")
    assert strategy._tradability_heartbeat_task is None


@pytest.mark.asyncio
async def test_composable_strategy_defaults_to_new_minute_entry_gate_for_qqq_parity_policy():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_entry_minute_gate_default_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_entry_minute_gate_default_001",
        extras={
            "strategy_template": "breakout_equity",
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._entry_check_on_new_minute_only() is True


@pytest.mark.asyncio
async def test_composable_strategy_allows_explicit_disable_of_new_minute_entry_gate():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_entry_minute_gate_override_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_entry_minute_gate_override_001",
        extras={
            "strategy_template": "breakout_equity",
            "entry_check_on_new_minute_only": False,
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._entry_check_on_new_minute_only() is False


@pytest.mark.asyncio
async def test_composable_strategy_defaults_to_new_minute_entry_gate_for_spy_parity_policy():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_entry_minute_gate_spy_default_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_entry_minute_gate_spy_default_001",
        extras={
            "strategy_template": "spy_intraday_core",
            "universe": [{"signal_symbol": "SPY", "long_exec_symbol": "SPXL", "short_exec_symbol": "SPXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._entry_check_on_new_minute_only() is True


@pytest.mark.asyncio
async def test_composable_strategy_short_circuits_non_signal_exec_tick_without_active_lot():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_non_signal_short_circuit_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_non_signal_short_circuit_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    stage_exec = AsyncMock(return_value=False)
    strategy._execute_runtime_stage = stage_exec

    await strategy.on_features(
        "SOXS",
        {
            "ts_epoch_s": 1_775_600_000.0,
            "last_price": 40.0,
            "session": "OVERNIGHT",
        },
    )

    stage_exec.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_keeps_runtime_stages_for_non_signal_exec_tick_with_active_lot():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_non_signal_active_lot_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_non_signal_active_lot_001",
        extras={
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._has_active_exec_lot = lambda _symbol: True
    stage_exec = AsyncMock(return_value=True)
    strategy._execute_runtime_stage = stage_exec

    await strategy.on_features(
        "SOXS",
        {
            "ts_epoch_s": 1_775_600_000.0,
            "last_price": 40.0,
            "session": "OVERNIGHT",
        },
    )

    stage_exec.assert_called()


@pytest.mark.asyncio
async def test_composable_strategy_offsession_idle_runs_housekeeping_stages_only():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_signal_offsession_short_circuit_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_signal_offsession_short_circuit_001",
        extras={
            "gate_pipeline": ["session_window"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["RTH"]}}},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    stage_exec = AsyncMock(return_value=False)
    strategy._execute_runtime_stage = stage_exec

    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 1_775_600_000.0,
            "last_price": 40.0,
            "session": "OVERNIGHT",
        },
    )

    stage_ids = [str(call.kwargs.get("stage_id") or "") for call in stage_exec.call_args_list]
    assert stage_ids == [
        "runtime_recovery",
        "broker_truth_sync",
        "safety_invariants",
        "session_flatten",
    ]


@pytest.mark.asyncio
async def test_composable_strategy_offsession_short_circuit_audit_dedupes_same_minute():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_signal_offsession_audit_dedupe_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_signal_offsession_audit_dedupe_001",
        extras={
            "gate_pipeline": ["session_window"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["RTH"]}}},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._execute_runtime_stage = AsyncMock(return_value=False)
    audits: list[tuple[str, str]] = []

    def _audit(stage, symbol, reason, payload):
        audits.append((str(stage), str(symbol)))

    strategy._audit = _audit
    ts_epoch_s = 1_775_600_000.0
    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": ts_epoch_s,
            "last_price": 40.0,
            "session": "OVERNIGHT",
        },
    )
    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": ts_epoch_s + 5.0,
            "last_price": 40.1,
            "session": "OVERNIGHT",
        },
    )

    offsession_rows = [row for row in audits if row[0] == "signal_offsession_idle_short_circuit"]
    assert len(offsession_rows) == 1


@pytest.mark.asyncio
async def test_composable_strategy_keeps_signal_tick_outside_session_when_active_lot_exists():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_signal_offsession_active_lot_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_signal_offsession_active_lot_001",
        extras={
            "gate_pipeline": ["session_window"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["RTH"]}}},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._has_active_exec_lot = lambda _symbol: True
    stage_exec = AsyncMock(return_value=True)
    strategy._execute_runtime_stage = stage_exec

    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 1_775_600_000.0,
            "last_price": 40.0,
            "session": "OVERNIGHT",
        },
    )

    stage_exec.assert_called()


@pytest.mark.asyncio
async def test_composable_strategy_does_not_short_circuit_signal_tick_when_session_gate_disabled():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_signal_offsession_gate_disabled_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_signal_offsession_gate_disabled_001",
        extras={
            "gate_pipeline": ["session_window"],
            "gate_overrides": {
                "session_window": {
                    "enabled": False,
                    "params": {"allowed_sessions": ["RTH"]},
                }
            },
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    stage_exec = AsyncMock(return_value=True)
    strategy._execute_runtime_stage = stage_exec

    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 1_775_600_000.0,
            "last_price": 40.0,
            "session": "OVERNIGHT",
        },
    )

    stage_exec.assert_called()


@pytest.mark.asyncio
async def test_composable_strategy_does_not_short_circuit_signal_tick_when_session_missing():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_signal_missing_session_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_signal_missing_session_001",
        extras={
            "gate_pipeline": ["session_window"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["RTH"]}}},
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    stage_exec = AsyncMock(return_value=True)
    strategy._execute_runtime_stage = stage_exec

    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 1_775_600_000.0,
            "last_price": 40.0,
            # no session field
        },
    )

    stage_exec.assert_called()


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_interval_uses_offsession_override():
    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_broker_truth_sync_interval_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_broker_truth_sync_interval_001",
        extras={
            "broker_truth_sync_interval_sec_rth": 10,
            "broker_truth_sync_interval_sec_offsession": 75,
            "gate_pipeline": ["session_window"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["RTH"]}}},
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._broker_truth_sync_min_interval_sec(session="RTH", instrument=None) == pytest.approx(10.0)
    assert strategy._broker_truth_sync_min_interval_sec(session="OVERNIGHT", instrument=None) == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_skips_offsession_probe_within_interval():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(return_value=[])

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_test_broker_truth_sync_throttle_001",
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_broker_truth_sync_throttle_001",
        extras={
            "broker_truth_sync_interval_sec_rth": 10,
            "broker_truth_sync_interval_sec_offsession": 60,
            "gate_pipeline": ["session_window"],
            "gate_overrides": {"session_window": {"params": {"allowed_sessions": ["RTH"]}}},
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._last_broker_truth_sync_ts = 100.0
    strategy.broker_truth_sync_module = SimpleNamespace(evaluate=lambda **kwargs: SimpleNamespace(ok=True))

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="OVERNIGHT",
        now_ts_epoch_s=120.0,
    )

    adapter.open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_uses_fast_path_when_healthy():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(return_value=[])

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_fast_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_broker_truth_fast_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace()
    strategy.broker_truth_sync_module = SimpleNamespace(
        evaluate=lambda **kwargs: SimpleNamespace(ok=True, reason="ready", orphan_order_refs=(), missing_order_refs=(), freeze_symbols=())
    )

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=200.0,
    )

    adapter.open_orders.assert_awaited_once_with(force_refresh=False)


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_uses_cached_snapshot_when_fresh():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(return_value=[])

    captured_orders = []

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_cached_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_broker_truth_cached_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace()
    strategy._broker_open_orders_snapshot = (SimpleNamespace(order_ref="cached", symbol="SOXL"),)
    strategy._broker_open_orders_snapshot_ts = 190.0
    strategy.broker_truth_sync_module = SimpleNamespace(
        evaluate=lambda **kwargs: (
            captured_orders.append(tuple(kwargs.get("broker_open_orders") or ()))
            or SimpleNamespace(ok=True, reason="ready", orphan_order_refs=(), missing_order_refs=(), freeze_symbols=())
        )
    )

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=200.0,
    )

    adapter.open_orders.assert_not_awaited()
    assert len(captured_orders) == 1
    assert captured_orders[0][0].order_ref == "cached"


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_defers_stale_snapshot_mismatch_until_refresh():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(return_value=[SimpleNamespace(order_ref="fresh", symbol="SOXL")])

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_stale_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_broker_truth_stale_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace()
    strategy._broker_open_orders_snapshot = (SimpleNamespace(order_ref="stale", symbol="SOXL"),)
    strategy._broker_open_orders_snapshot_ts = 150.0
    strategy.broker_truth_sync_module = SimpleNamespace(
        evaluate=lambda **kwargs: SimpleNamespace(
            ok=False,
            reason="broker_truth_mismatch",
            orphan_order_refs=("stale",),
            missing_order_refs=(),
            freeze_symbols=("SOXL",),
        )
    )
    refresh_calls = []
    strategy._ensure_broker_open_orders_refresh = lambda **kwargs: refresh_calls.append(kwargs)
    mark_calls = []
    strategy.order_lifecycle_module = SimpleNamespace(mark_sync_issue=lambda symbol, reason: mark_calls.append((symbol, reason)))
    kill_calls = []
    strategy._activate_symbol_kill_switch = lambda **kwargs: kill_calls.append(kwargs)
    audit_stages = []
    strategy._audit = lambda **kwargs: audit_stages.append(kwargs.get("stage"))

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=200.0,
    )

    adapter.open_orders.assert_not_awaited()
    assert len(refresh_calls) == 1
    assert mark_calls == []
    assert kill_calls == []
    assert "broker_truth_sync_stale_snapshot_mismatch_deferred" in audit_stages


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_escalates_force_refresh_on_mismatch():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(side_effect=[[SimpleNamespace(order_ref="a", symbol="SOXL")], [SimpleNamespace(order_ref="a", symbol="SOXL")]])

    reports = [
        SimpleNamespace(ok=False, reason="broker_truth_mismatch", orphan_order_refs=("a",), missing_order_refs=(), freeze_symbols=("SOXL",)),
        SimpleNamespace(ok=True, reason="ready", orphan_order_refs=(), missing_order_refs=(), freeze_symbols=()),
    ]

    class _Module:
        def evaluate(self, **kwargs):
            return reports.pop(0)

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_escalate_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_broker_truth_escalate_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace()
    strategy.broker_truth_sync_module = _Module()
    mark_calls = []
    strategy.order_lifecycle_module = SimpleNamespace(mark_sync_issue=lambda symbol, reason: mark_calls.append((symbol, reason)))
    kill_calls = []
    strategy._activate_symbol_kill_switch = lambda **kwargs: kill_calls.append(kwargs)

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=200.0,
    )

    assert adapter.open_orders.await_args_list[0].kwargs == {"force_refresh": False}
    assert adapter.open_orders.await_args_list[1].kwargs == {"force_refresh": True}
    assert mark_calls == []
    assert kill_calls == []


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_does_not_freeze_when_confirmation_probe_unavailable():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(side_effect=[[SimpleNamespace(order_ref="a", symbol="SOXL")], RuntimeError("ib_timeout")])

    reports = [
        SimpleNamespace(ok=False, reason="broker_truth_mismatch", orphan_order_refs=("a",), missing_order_refs=(), freeze_symbols=("SOXL",)),
    ]

    class _Module:
        def evaluate(self, **kwargs):
            return reports.pop(0)

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_deferred_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_broker_truth_deferred_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace()
    strategy.broker_truth_sync_module = _Module()
    mark_calls = []
    strategy.order_lifecycle_module = SimpleNamespace(mark_sync_issue=lambda symbol, reason: mark_calls.append((symbol, reason)))
    kill_calls = []
    strategy._activate_symbol_kill_switch = lambda **kwargs: kill_calls.append(kwargs)
    audit_stages = []
    strategy._audit = lambda **kwargs: audit_stages.append(kwargs.get("stage"))

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=250.0,
    )

    assert adapter.open_orders.await_args_list[0].kwargs == {"force_refresh": False}
    assert adapter.open_orders.await_args_list[1].kwargs == {"force_refresh": True}
    assert mark_calls == []
    assert kill_calls == []
    assert "broker_truth_sync_confirmation_deferred" in audit_stages


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_defers_freeze_until_confirmation_threshold():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(
                side_effect=[
                    [SimpleNamespace(order_ref="a", symbol="SOXL")],
                    [SimpleNamespace(order_ref="a", symbol="SOXL")],
                    [SimpleNamespace(order_ref="a", symbol="SOXL")],
                    [SimpleNamespace(order_ref="a", symbol="SOXL")],
                ]
            )

    report = SimpleNamespace(ok=False, reason="broker_truth_mismatch", orphan_order_refs=("a",), missing_order_refs=(), freeze_symbols=("SOXL",))

    class _Module:
        def evaluate(self, **kwargs):
            return report

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_threshold_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_threshold_001",
            extras={
                "gate_pipeline": ["session_window"],
                "broker_truth_sync_mismatch_confirmations_required": 2,
            },
        ),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace()
    strategy.broker_truth_sync_module = _Module()
    mark_calls = []
    strategy.order_lifecycle_module = SimpleNamespace(mark_sync_issue=lambda symbol, reason: mark_calls.append((symbol, reason)))
    kill_calls = []
    strategy._activate_symbol_kill_switch = lambda **kwargs: kill_calls.append(kwargs)

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=100.0,
    )
    assert mark_calls == []
    assert kill_calls == []

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="RTH",
        now_ts_epoch_s=111.0,
    )
    assert mark_calls == [("SOXL", "broker_truth_mismatch")]
    assert kill_calls == [{"symbol": "SOXL", "reason": "broker_truth_mismatch"}]


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_sync_skips_probe_when_idle_without_pending_activity():
    class _Adapter:
        def __init__(self):
            self.open_orders = AsyncMock(return_value=[])

    class _Ledger:
        def list_lots(self, **kwargs):
            return []

    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_broker_truth_idle_skip_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_broker_truth_idle_skip_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    adapter = _Adapter()
    strategy._resolve_broker_adapter = lambda: adapter
    strategy._runtime_exec_symbols = lambda **kwargs: ["SOXL"]
    strategy.execution = SimpleNamespace(ledger=_Ledger())
    strategy.broker_truth_sync_module = SimpleNamespace(
        evaluate=lambda **kwargs: SimpleNamespace(ok=True, reason="ready", orphan_order_refs=(), missing_order_refs=(), freeze_symbols=())
    )

    await strategy._maybe_run_broker_truth_sync(
        signal_symbol="SMH",
        instrument=None,
        session="OVERNIGHT",
        now_ts_epoch_s=200.0,
    )

    adapter.open_orders.assert_not_awaited()
    assert strategy._last_broker_truth_sync_ts == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_composable_strategy_eod_flatten_requires_explicit_same_day_contract():
    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_eod_default_flatten_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
            system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        ),
        params=SimpleNamespace(strategy_id="kernel_test_eod_default_flatten_001", extras={"gate_pipeline": ["session_window"]}),
    )
    await strategy.initialize()
    ts_epoch_s = datetime(2026, 4, 10, 15, 59, tzinfo=ET).timestamp()

    due, cutoff = strategy.session_module.eod_flatten_due(ts_epoch_s=ts_epoch_s)

    assert due is False
    assert cutoff == ""
    await strategy.on_shutdown("test_done")
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_eod_flatten_default_can_be_disabled():
    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_eod_default_flatten_disable_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
            system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        ),
        params=SimpleNamespace(
            strategy_id="kernel_test_eod_default_flatten_disable_001",
            extras={
                "gate_pipeline": ["session_window"],
                "enforce_same_day_flatten": False,
            },
        ),
    )
    await strategy.initialize()
    ts_epoch_s = datetime(2026, 4, 10, 16, 5, tzinfo=ET).timestamp()

    due, cutoff = strategy.session_module.eod_flatten_due(ts_epoch_s=ts_epoch_s)

    assert due is False
    assert cutoff == ""
    await strategy.on_shutdown("test_done")


@pytest.mark.asyncio
async def test_composable_strategy_eod_flatten_requires_explicit_cutoff_even_when_enforce_flag_is_true():
    strategy = ComposableStocksKernelStrategy(
        ctx=SimpleNamespace(
            strategy_id="kernel_test_eod_same_day_contract_001",
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
            metrics=SimpleNamespace(),
            engine=SimpleNamespace(),
            system_health=SimpleNamespace(subscribe=AsyncMock(return_value=None)),
        ),
        params=SimpleNamespace(
            strategy_id="kernel_test_eod_same_day_contract_001",
            extras={
                "gate_pipeline": ["session_window"],
                "enforce_same_day_flatten": True,
            },
        ),
    )
    await strategy.initialize()
    ts_epoch_s = datetime(2026, 4, 10, 15, 59, tzinfo=ET).timestamp()

    due, cutoff = strategy.session_module.eod_flatten_due(ts_epoch_s=ts_epoch_s)

    assert due is False
    assert cutoff == ""
def test_stocks_kernel_publish_and_clear_central_data_gate():
    class _Ctx:
        def __init__(self):
            self.reported = []
            self.cleared = []

        def report_data_unavailable(self, payload):
            self.reported.append(dict(payload))
            return {"ok": False}

        def clear_data_unavailable(self, payload):
            self.cleared.append(dict(payload))

    ctx = _Ctx()
    strategy = ComposableStocksKernelStrategy.__new__(ComposableStocksKernelStrategy)
    strategy.ctx = ctx
    strategy.strategy_id = "stocks_kernel_shadow_smh_001"
    strategy._central_data_gate_active = set()
    strategy._audit = lambda **kwargs: None

    strategy._publish_central_data_gate(
        symbol="SMH",
        reason="history_missing",
        stage="required_data_missing",
        payload={"history_ready": False},
    )
    strategy._publish_central_data_gate(
        symbol="SMH",
        reason="feature_seed_missing",
        stage="feature_not_ready",
        payload={"v3m": "feature_seed_missing"},
    )
    strategy._clear_central_data_gates(symbol="SMH")

    assert [item["stage"] for item in ctx.reported] == ["required_data_missing", "feature_not_ready"]
    assert {item["reason"] for item in ctx.reported} == {"history_missing", "feature_seed_missing"}
    assert all(item["dataset"] == "strategy_tradability" for item in ctx.reported)
    assert all(item["severity"] == "entry_blocking" for item in ctx.reported)
    assert [item["stage"] for item in ctx.cleared] == ["feature_not_ready", "required_data_missing"] or {
        item["stage"] for item in ctx.cleared
    } == {"feature_not_ready", "required_data_missing"}
