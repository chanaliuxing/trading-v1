from types import SimpleNamespace

import pytest

from strategies.options_plugins.qqq_1dte_ma10_reject import QQQ1DTEMa10RejectPlugin
from strategies.options_plugins.spy_1dte_ma10_reject import SPY1DTEMa10RejectPlugin
from strategies.options_plugins.spy_intraday_momentum_option import SPYIntradayMomentumOptionPlugin
from strategies.options_plugins.ma_option_definition_plugin_base import DefinitionDrivenPureOptionPlugin
from strategies.options_kernel.modules import definition_registry
from strategies.ma_1dte_option_live_base import MA1DTEMA10RejectLiveStrategyBase
from strategies.qqq_1dte_ma10_reject_live_strategy import QQQ1DTEMA10RejectLiveStrategy
from strategies.spy_1dte_ma10_reject_live_strategy import SPY1DTEMA10RejectLiveStrategy


def test_plugins_bind_to_expected_definitions() -> None:
    qqq = QQQ1DTEMa10RejectPlugin()
    spy = SPY1DTEMa10RejectPlugin()
    spy_intraday = SPYIntradayMomentumOptionPlugin()

    assert qqq._definition_id() == "QQQ_MA_CROSS_STACK_CONT_V2_DUAL"
    assert spy._definition_id() == "SPY_MA_CROSS_STACK_CONT_V2_DUAL"
    assert spy_intraday._definition_id() == "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"

    qqq_def = definition_registry.get(qqq._definition_id())
    spy_def = definition_registry.get(spy._definition_id())
    spy_intraday_def = definition_registry.get(spy_intraday._definition_id())

    assert qqq_def.underlying == "QQQ"
    assert spy_def.underlying == "SPY"
    assert spy_intraday_def.underlying == "SPY"
    assert qqq_def.signal_module.module_id == "ma_cross_stack_cont_v2_dual_exp"
    assert spy_def.signal_module.module_id == "ma_cross_stack_cont_v2_dual_exp"
    assert spy_intraday_def.signal_module.module_id == "generic_vwap_momentum_v1"


def test_plugins_default_to_pure_plugin_kernel_mode() -> None:
    qqq = QQQ1DTEMa10RejectPlugin()
    spy = SPY1DTEMa10RejectPlugin()

    assert qqq.apply_default_extras({})["kernel_mode"] == "pure_plugin"
    assert spy.apply_default_extras({})["kernel_mode"] == "pure_plugin"


def test_qqq_plugin_initializes_pure_state_fields() -> None:
    qqq = QQQ1DTEMa10RejectPlugin()

    assert qqq._pure_trades_filled_today == 0
    assert qqq._pure_quote_health_block_day is False


def test_qqq_plugin_uses_definition_driven_base() -> None:
    assert issubclass(QQQ1DTEMa10RejectPlugin, DefinitionDrivenPureOptionPlugin)


def test_definition_plugins_publish_namespace_metadata() -> None:
    qqq = QQQ1DTEMa10RejectPlugin().apply_default_extras({})
    spy = SPY1DTEMa10RejectPlugin().apply_default_extras({})

    assert qqq["definition_id"] == "QQQ_MA_CROSS_STACK_CONT_V2_DUAL"
    assert spy["definition_id"] == "SPY_MA_CROSS_STACK_CONT_V2_DUAL"
    assert qqq["audit_namespace"] != spy["audit_namespace"]
    assert qqq["position_namespace"] != spy["position_namespace"]
    assert qqq["risk_symbol"] == "OPT:QQQ:1DTE"
    assert spy["risk_symbol"] == "OPT:SPY:1DTE"
    assert qqq.get("allow_stale_entry_quotes") is None
    assert spy["allow_stale_entry_quotes"] is True


def test_spy_plugin_no_longer_inherits_qqq_plugin() -> None:
    assert issubclass(SPY1DTEMa10RejectPlugin, DefinitionDrivenPureOptionPlugin)
    assert not issubclass(SPY1DTEMa10RejectPlugin, QQQ1DTEMa10RejectPlugin)


def test_spy_intraday_momentum_plugin_is_definition_driven() -> None:
    assert issubclass(SPYIntradayMomentumOptionPlugin, DefinitionDrivenPureOptionPlugin)
    extras = SPYIntradayMomentumOptionPlugin().apply_default_extras({})
    assert extras["definition_id"] == "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"
    assert extras["underlying"] == "SPY"
    assert extras["kernel_mode"] == "pure_plugin"


def test_live_strategies_share_base_without_inheriting_each_other() -> None:
    assert issubclass(QQQ1DTEMA10RejectLiveStrategy, MA1DTEMA10RejectLiveStrategyBase)
    assert issubclass(SPY1DTEMA10RejectLiveStrategy, MA1DTEMA10RejectLiveStrategyBase)
    assert not issubclass(SPY1DTEMA10RejectLiveStrategy, QQQ1DTEMA10RejectLiveStrategy)


def test_live_wrappers_publish_distinct_runtime_defaults() -> None:
    qqq = QQQ1DTEMA10RejectLiveStrategy.__new__(QQQ1DTEMA10RejectLiveStrategy)
    qqq.strategy_id = "qqq_1dte_ma10_reject_0324_v2_001"
    spy = SPY1DTEMA10RejectLiveStrategy.__new__(SPY1DTEMA10RejectLiveStrategy)
    spy.strategy_id = "spy_1dte_ma10_reject_0324_001"

    assert qqq._strategy_slug() == "qqq_1dte_ma10_reject"
    assert spy._strategy_slug() == "spy_1dte_ma10_reject"
    assert qqq._default_underlying() == "QQQ"
    assert spy._default_underlying() == "SPY"
    assert qqq._default_replay_strategy_id() == "ma10_reject_0324"
    assert spy._default_replay_strategy_id() == "ma_cross_stack_cont_v2_dual_exp"
    assert qqq._state_filename() != spy._state_filename()


@pytest.mark.asyncio
async def test_initialize_pure_promotes_position_namespace_to_strategy_instance() -> None:
    spy = SPY1DTEMa10RejectPlugin()
    params = SimpleNamespace(extras={})
    ctx = SimpleNamespace(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
        history=lambda *args, **kwargs: [],
        subscribe_features=lambda *args, **kwargs: None,
    )

    async def _history(*args, **kwargs):
        return []

    async def _subscribe(*args, **kwargs):
        return None

    ctx.history = _history
    ctx.subscribe_features = _subscribe
    kernel = SimpleNamespace(preload_bar_history=lambda *args, **kwargs: 0)

    await spy.initialize_pure(ctx, params, kernel)

    assert params.extras["position_namespace"] == "optk.spy_ma_cross_stack_cont_v2_dual.spy_1dte_ma10_reject_0324_001"


@pytest.mark.asyncio
async def test_same_definition_multiple_instances_get_distinct_position_namespaces() -> None:
    async def _history(*args, **kwargs):
        return []

    async def _subscribe(*args, **kwargs):
        return None

    kernel = SimpleNamespace(preload_bar_history=lambda *args, **kwargs: 0)
    namespaces = []
    for strategy_id in ("spy_1dte_ma10_reject_0324_001", "spy_1dte_ma10_reject_0324_002"):
        plugin = SPY1DTEMa10RejectPlugin()
        params = SimpleNamespace(extras={})
        ctx = SimpleNamespace(
            strategy_id=strategy_id,
            log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
            history=_history,
            subscribe_features=_subscribe,
        )
        await plugin.initialize_pure(ctx, params, kernel)
        namespaces.append(params.extras["position_namespace"])

    assert namespaces[0] != namespaces[1]
