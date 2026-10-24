import pytest

from libs.strategy.base import DecisionEnvelope, Freshness
from strategies.stocks_kernel.builder import (
    StrategyInstanceBlueprint,
    StrategyInstanceSpec,
    build_manifest_strategy_instance_config,
    build_plugin_strategy_blueprint,
    build_strategy_blueprint,
    build_strategy_extras,
    build_strategy_instance_config,
    template_catalog,
)
from types import SimpleNamespace
from strategies.stocks_kernel.plugin import BaseStockPlugin
from strategies.stocks_kernel.plugins.example_breakout import ExampleBreakoutStockPlugin
from strategies.stocks_kernel.plugins.nvda_minute_flip import NVDAMinuteFlipStockPlugin
import strategies.stocks_kernel.strategy as stocks_strategy_mod
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy

from strategies.stocks_kernel.config_loader import load_kernel_config
from strategies.stocks_kernel.data.profiles import get_data_profile
from strategies.stocks_kernel.templates import default_template_registry
from strategies.stocks_kernel.types import KernelFillReport
from tool.report.build_stocks_kernel_parity_report import build_stocks_kernel_parity_report


def test_data_profile_lookup_and_parity_report():
    profile = get_data_profile("equity_rth_standard")
    report = build_stocks_kernel_parity_report(
        expected=[{"stage": "gate", "reason": "ready"}, {"stage": "entry", "side": "LONG"}],
        actual=[{"stage": "gate", "reason": "ready"}, {"stage": "entry", "side": "SHORT"}],
    )

    assert profile.max_staleness_sec == 3.0
    assert profile.min_history_bars == 15
    assert report["matched_count"] == 1
    assert report["match_ratio"] == 0.5


def test_template_registry_and_config_loader_apply_template_defaults():
    registry = default_template_registry()
    template = registry.get("trend_follow_equity")
    params = SimpleNamespace(
        extras={
            "strategy_template": "trend_follow_equity",
            "universe": [{"signal_symbol": "SPY", "long_exec_symbol": "SPXL", "short_exec_symbol": "SPXS"}],
        }
    )

    config = load_kernel_config("spy_kernel_001", params)

    assert template.policy_id == "qqq_intraday_policy_v1"
    assert config.data_profile == "default"
    assert "distance_from_vwap_atr" in config.feature_set
    assert "risk_budget" in config.gate_pipeline
    assert config.feature_params["distance_from_vwap_atr"]["window"] == 14


def test_spy_intraday_template_clones_qqq_logic_with_spy_policy_identity():
    registry = default_template_registry()
    template = registry.get("spy_intraday_core")
    params = SimpleNamespace(
        extras={
            "strategy_template": "spy_intraday_core",
            "universe": [{"signal_symbol": "SPY", "long_exec_symbol": "SPXL", "short_exec_symbol": "SPXS"}],
        }
    )

    config = load_kernel_config("spy_kernel_parity_001", params)

    assert template.policy_id == "spy_intraday_policy_v1"
    assert config.policy_id == "spy_intraday_policy_v1"
    assert config.policy_params["rule_engine_parity"] is True
    assert config.instruments[0].signal_symbol == "SPY"
    assert config.instruments[0].long_exec_symbol == "SPXL"
    assert config.instruments[0].short_exec_symbol == "SPXS"


def test_strategy_instance_blueprint_builds_template_driven_extras():
    extras = build_strategy_extras(
        StrategyInstanceBlueprint(
            strategy_template="offhours_equity",
            universe=[{"signal_symbol": "SPY", "long_exec_symbol": "SPXL", "short_exec_symbol": "SPXS"}],
            dry_run=False,
            gate_overrides={"direction_match": {"min_abs_value": 0.02}},
            policy_params={"hard_stop_atr_mult": 0.7},
        )
    )

    assert extras["strategy_template"] == "offhours_equity"
    assert extras["dry_run"] is False
    assert extras["data_profile"] == "equity_offhours_standard"
    assert extras["policy_params"]["hard_stop_atr_mult"] == 0.7
    assert extras["gate_overrides"]["direction_match"]["min_abs_value"] == 0.02


def test_strategy_blueprint_builder_normalizes_symbols_and_catalog_exposes_templates():
    blueprint = build_strategy_blueprint(
        strategy_template="trend_follow_equity",
        signal_symbol="smh",
        long_exec_symbol="soxl",
        short_exec_symbol="soxs",
        confirm_symbol="smh",
    )
    extras = build_strategy_extras(blueprint)
    catalog = template_catalog()

    assert extras["universe"][0]["signal_symbol"] == "SMH"
    assert extras["universe"][0]["long_exec_symbol"] == "SOXL"
    assert extras["universe"][0]["short_exec_symbol"] == "SOXS"
    assert any(item["template_id"] == "trend_follow_equity" for item in catalog)
    assert any(item["template_id"] == "spy_intraday_core" for item in catalog)


def test_strategy_blueprint_validation_rejects_same_exec_symbols():
    with pytest.raises(ValueError, match="must differ"):
        build_strategy_extras(
            StrategyInstanceBlueprint(
                strategy_template="trend_follow_equity",
                universe=[{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXL"}],
            )
        )


def test_build_strategy_instance_config_returns_minimal_loadable_shape():
    instance = build_strategy_instance_config(
        StrategyInstanceSpec(
            strategy_id="stocks_kernel_smh_001",
            strategy_template="breakout_equity",
            signal_symbol="SMH",
            long_exec_symbol="SOXL",
            short_exec_symbol="SOXS",
            enabled=False,
            dry_run=True,
            capital_limit_usd=50000,
        )
    )

    assert instance["id"] == "stocks_kernel_smh_001"
    assert instance["module"] == "strategies.stocks_kernel_strategy"
    assert instance["class"] == "StocksKernelStrategy"
    assert instance["params"]["extras"]["strategy_template"] == "breakout_equity"
    assert instance["params"]["extras"]["universe"][0]["signal_symbol"] == "SMH"


class _DemoStockPlugin(BaseStockPlugin):
    component_name = "demo_stock_plugin"


class _BlockingStockPlugin(BaseStockPlugin):
    component_name = "blocking_stock_plugin"

    async def on_intent(self, *, strategy, envelope, symbol, side):
        envelope.extras["stock_plugin_block_reason"] = "test_plugin_block"
        return False


class _FillAttributionPlugin(BaseStockPlugin):
    def __init__(self, component_name):
        self.component_name = component_name
        self.fills = []

    async def on_fill(self, *, strategy, fill, apply_result=None):
        self.fills.append(
            {
                "strategy_id": strategy.strategy_id,
                "symbol": fill.symbol,
                "apply_result": {"tag": apply_result.tag, "symbol": apply_result.symbol},
            }
        )


class _FakeCounter:
    def __init__(self):
        self.calls = []

    def labels(self, **labels):
        self.calls.append(("labels", labels))
        return self

    def inc(self):
        self.calls.append(("inc", None))


class _FakeExecution:
    def __init__(self, tag):
        self.tag = tag
        self.bindings = SimpleNamespace(resolve=lambda **kwargs: None)
        self.ledger = SimpleNamespace(get_lot=lambda lot_id: None)

    def apply_fill(self, fill):
        return SimpleNamespace(
            position_lot_id=None,
            order_role=None,
            action="ignored",
            reason="test_apply_fill",
            tag=self.tag,
            symbol=fill.symbol,
        )


def test_build_plugin_strategy_blueprint_embeds_plugin_metadata():
    blueprint = build_plugin_strategy_blueprint(
        plugin=_DemoStockPlugin(),
        strategy_id="stocks_kernel_demo_001",
        strategy_template="breakout_equity",
        signal_symbol="QQQ",
        long_exec_symbol="TQQQ",
        short_exec_symbol="SQQQ",
        extra_extras={"shared_underlying_namespace": "qqq"},
    )

    extras = build_strategy_extras(blueprint)

    assert extras["plugin_component"] == "demo_stock_plugin"
    assert extras["plugin_policy_id"] == "demo_stock_plugin"
    assert extras["plugin_policy_payload"]["strategy_id"] == "stocks_kernel_demo_001"
    assert extras["plugin_policy_payload"]["plugin_component"] == "demo_stock_plugin"
    assert extras["plugin_cls"].endswith(":_DemoStockPlugin")


def test_stock_plugin_declares_lifecycle_hooks():
    plugin = ExampleBreakoutStockPlugin()

    for hook_name in ("on_init", "on_features", "on_intent", "on_fill", "on_shutdown"):
        assert callable(getattr(plugin, hook_name))


def test_nvda_plugin_manifest_generates_full_production_config():
    cfg = build_manifest_strategy_instance_config(
        plugin=NVDAMinuteFlipStockPlugin(order_type="LMT", limit_offset_bp=20),
        strategy_id="nvda_minute_flip_paper_001",
        enabled=True,
        capital_limit_usd=250,
        qty=1,
        plugin_params={"order_type": "LMT", "limit_offset_bp": 20},
    )

    extras = cfg["params"]["extras"]
    assert cfg["registry_name"] == "stocks_kernel"
    assert extras["plugin_cls"] == "strategies.stocks_kernel.plugins.nvda_minute_flip:NVDAMinuteFlipStockPlugin"
    assert extras["data_contract"]["symbols"] == ["NVDA"]
    assert extras["production_manifest"]["entry_contract"] == {"uses_decision_pipeline": True, "uom_only": True}
    assert extras["production_manifest"]["recovery_contract"]["unknown_owner_action"] == "quarantine"
    assert extras["production_manifest"]["eod_contract"]["no_overnight"] is True
    assert extras["production_manifest"]["tradability_reporting"]["reports_strategy_tradability_gate"] is True
    assert extras["policy_registry_specs"][0]["policy_id"] == "nvda_minute_flip_policy"
    assert extras["policy_params"]["symbol"] == "NVDA"
    assert extras["policy_params"]["qty"] == 1.0
    assert extras["execution_profile"]["protection_enabled"] is True


@pytest.mark.asyncio
async def test_composable_strategy_loads_and_runs_stock_plugin_hook():
    ctx = SimpleNamespace(
        strategy_id="stocks_kernel_plugin_test_001",
        log=None,
        metrics=None,
        engine=None,
    )
    params = SimpleNamespace(extras={})
    strategy = ComposableStocksKernelStrategy(ctx, params)
    plugin = strategy._load_stock_plugin(
        extras={"plugin_cls": "strategies.stocks_kernel.plugins.example_breakout:ExampleBreakoutStockPlugin"}
    )
    strategy._stock_plugin = plugin
    strategy._stock_plugin_component = plugin.component_name

    updated = await strategy._call_stock_plugin_hook("on_features", symbol="QQQ", features={"last_price": 100.0})

    assert isinstance(plugin, ExampleBreakoutStockPlugin)
    assert updated["last_price"] == 100.0
    assert updated["stock_plugin_component"] == "example_breakout_stock_plugin"


@pytest.mark.asyncio
async def test_stock_plugin_intent_block_metric_includes_plugin_and_reason(monkeypatch):
    fake_counter = _FakeCounter()
    monkeypatch.setattr(stocks_strategy_mod, "exec_stock_plugin_intent_blocks_total", fake_counter)
    strategy = ComposableStocksKernelStrategy(
        SimpleNamespace(strategy_id="stocks_kernel_block_plugin_001", log=None, metrics=None, engine=None),
        SimpleNamespace(extras={}),
    )
    strategy._stock_plugin = _BlockingStockPlugin()
    strategy._stock_plugin_component = strategy._stock_plugin.component_name
    envelope = DecisionEnvelope(
        decision_id="d-plugin-block-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=10,
        freshness=Freshness(ttl_ms=1000, dlb_ms=1000),
        extras={"trace_id": "trace-plugin-block"},
    )

    await strategy._submit_intent(envelope=envelope, symbol="TQQQ", side="BUY")

    assert ("inc", None) in fake_counter.calls
    assert (
        "labels",
        {
            "strategy_id": "stocks_kernel_block_plugin_001",
            "plugin_component": "blocking_stock_plugin",
            "symbol": "TQQQ",
            "reason": "test_plugin_block",
        },
    ) in fake_counter.calls


@pytest.mark.asyncio
async def test_two_stock_plugins_keep_fill_attribution_isolated():
    strategy_a = ComposableStocksKernelStrategy(
        SimpleNamespace(strategy_id="stocks_kernel_plugin_a_001", log=None, metrics=None, engine=None),
        SimpleNamespace(extras={}),
    )
    strategy_b = ComposableStocksKernelStrategy(
        SimpleNamespace(strategy_id="stocks_kernel_plugin_b_001", log=None, metrics=None, engine=None),
        SimpleNamespace(extras={}),
    )
    plugin_a = _FillAttributionPlugin("fill_plugin_a")
    plugin_b = _FillAttributionPlugin("fill_plugin_b")
    strategy_a._stock_plugin = plugin_a
    strategy_a._stock_plugin_component = plugin_a.component_name
    strategy_a.execution = _FakeExecution("a")
    strategy_b._stock_plugin = plugin_b
    strategy_b._stock_plugin_component = plugin_b.component_name
    strategy_b.execution = _FakeExecution("b")

    await strategy_a.on_fill_received(KernelFillReport(symbol="TQQQ", qty=1, price=100.0))
    await strategy_b.on_fill_received(KernelFillReport(symbol="SQQQ", qty=2, price=20.0))

    assert plugin_a.fills == [
        {
            "strategy_id": "stocks_kernel_plugin_a_001",
            "symbol": "TQQQ",
            "apply_result": {"tag": "a", "symbol": "TQQQ"},
        }
    ]
    assert plugin_b.fills == [
        {
            "strategy_id": "stocks_kernel_plugin_b_001",
            "symbol": "SQQQ",
            "apply_result": {"tag": "b", "symbol": "SQQQ"},
        }
    ]


def test_breakout_template_enables_qqq_parity_defaults():
    params = SimpleNamespace(
        extras={
            "strategy_template": "breakout_equity",
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        }
    )
    config = load_kernel_config("smh_kernel_qqq_parity_001", params)

    assert config.policy_id == "qqq_intraday_policy_v1"
    assert config.policy_params["rule_engine_parity"] is True
    assert config.policy_params["entry_check_on_new_minute_only"] is True


def test_config_loader_accepts_direct_symbol_shorthand():
    params = SimpleNamespace(
        extras={
            "strategy_template": "mean_revert_equity",
            "signal_symbol": "smh",
            "long_exec_symbol": "soxl",
            "short_exec_symbol": "soxs",
        }
    )

    config = load_kernel_config("smh_kernel_001", params)

    assert len(config.instruments) == 1
    assert config.instruments[0].signal_symbol == "SMH"
    assert config.instruments[0].long_exec_symbol == "SOXL"
    assert config.instruments[0].short_exec_symbol == "SOXS"


def test_config_loader_defaults_require_exec_symbol_snapshot_for_live_instances():
    params = SimpleNamespace(
        extras={
            "dry_run": False,
            "strategy_template": "mean_revert_equity",
            "signal_symbol": "smh",
            "long_exec_symbol": "soxl",
            "short_exec_symbol": "soxs",
        }
    )

    config = load_kernel_config("smh_kernel_live_001", params)

    assert config.raw_extras["require_exec_symbol_snapshot"] is True


def test_config_loader_parses_execution_lanes_with_legacy_fields_compatible():
    params = SimpleNamespace(
        extras={
            "strategy_template": "trend_follow_equity",
            "universe": [
                {
                    "signal_symbol": "SMH",
                    "long_exec_symbol": "SOXL",
                    "short_exec_symbol": "SOXS",
                    "execution_lanes": [
                        {"lane_id": "alpha_long", "symbol": "SOXL", "side": "LONG", "role": "execution"},
                        {"lane_id": "alpha_short", "symbol": "SOXS", "side": "SHORT", "role": "execution"},
                        {"lane_id": "hedge", "symbol": "SMH", "role": "hedge"},
                    ],
                }
            ],
        }
    )

    config = load_kernel_config("smh_kernel_lane_001", params)
    instrument = config.instruments[0]

    assert instrument.long_exec_symbol == "SOXL"
    assert instrument.short_exec_symbol == "SOXS"
    assert instrument.exec_symbols() == ("SOXL", "SOXS")
    assert instrument.lane_for_side("LONG").lane_id == "alpha_long"
    assert instrument.lane_for_side("SHORT").lane_id == "alpha_short"
