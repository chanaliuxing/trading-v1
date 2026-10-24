from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyParams
from strategies.options_universal.builder import (
    build_option_strategy_instance_config,
    load_option_plugin,
    validate_option_strategy_config,
)
from strategies.options_universal.base_strategy import UniversalOptionLiveStrategy
from strategies.options_universal.plugin import OptionStrategyManifest, OptionStrategyPlugin
from strategies.options_universal.template_plugin import TemplateOptionPlugin
from strategies.options_universal.types import OptionQuoteSelection


ET = ZoneInfo("America/New_York")


class _DummyPlugin(OptionStrategyPlugin):
    component_name = "dummy_plugin"

    def __init__(self, *, handle_enter: bool, handle_sync: bool, quote: OptionQuoteSelection | None):
        self.handle_enter = handle_enter
        self.handle_sync = handle_sync
        self.quote = quote

    async def maybe_enter(self, strategy, ctx):
        return self.handle_enter

    async def sync_and_manage_position(self, strategy, ctx):
        return self.handle_sync

    def select_quote(self, strategy, req):
        return self.quote


class _BadManifestPlugin(OptionStrategyPlugin):
    component_name = "bad_manifest_plugin"

    def production_manifest(self) -> OptionStrategyManifest:
        return OptionStrategyManifest(
            strategy_key="bad_option",
            underlying="",
            trigger_source_ids=(),
            allowed_rights=("BAD",),
            max_spread_pct=0,
            quote_ttl_sec=0,
            contracts=0,
            no_new_entry_after="15:59:00",
            force_flatten_time="15:00:00",
        )


class _TestStrategy(UniversalOptionLiveStrategy):
    def __init__(self, ctx, params, plugin):
        self._test_plugin = plugin
        super().__init__(ctx, params)

    def build_plugin(self):
        return self._test_plugin


@pytest.mark.asyncio
async def test_plugin_hooks_can_fully_handle_paths(monkeypatch):
    ctx = MagicMock()
    params = StrategyParams()
    plugin = _DummyPlugin(
        handle_enter=True,
        handle_sync=True,
        quote=OptionQuoteSelection(quote={"opt_symbol": "OPT:SPY:20260325:590:C", "mid": 1.23}, reject_reason=None),
    )
    strat = _TestStrategy(ctx, params, plugin)
    strat._plugin = plugin

    super_enter = AsyncMock()
    super_sync = AsyncMock(return_value=False)
    monkeypatch.setattr("strategies.qqq_0dte_opt_live_strategy.QQQ0DTEOptionsLiveStrategy._maybe_enter", super_enter)
    monkeypatch.setattr("strategies.qqq_0dte_opt_live_strategy.QQQ0DTEOptionsLiveStrategy._sync_and_manage_position", super_sync)

    now = datetime(2026, 3, 25, 10, 0, tzinfo=ET)
    bar = {"close": 1.0, "vwap": 1.0}
    await strat._maybe_enter(now, bar)
    handled = await strat._sync_and_manage_position(now, bar)
    quote, reason = strat._select_0dte_quote(now, side="CALL", spot=590.0, spread_max_pct=0.2)

    assert handled is True
    assert super_enter.await_count == 0
    assert super_sync.await_count == 0
    assert quote is not None and quote["opt_symbol"].startswith("OPT:SPY:")
    assert reason is None


@pytest.mark.asyncio
async def test_plugin_fallback_uses_shared_kernel(monkeypatch):
    ctx = MagicMock()
    params = StrategyParams()
    plugin = _DummyPlugin(handle_enter=False, handle_sync=False, quote=None)
    strat = _TestStrategy(ctx, params, plugin)
    strat._plugin = plugin

    super_enter = AsyncMock()
    super_sync = AsyncMock(return_value=True)
    monkeypatch.setattr("strategies.qqq_0dte_opt_live_strategy.QQQ0DTEOptionsLiveStrategy._maybe_enter", super_enter)
    monkeypatch.setattr("strategies.qqq_0dte_opt_live_strategy.QQQ0DTEOptionsLiveStrategy._sync_and_manage_position", super_sync)

    now = datetime(2026, 3, 25, 10, 0, tzinfo=ET)
    bar = {"close": 1.0, "vwap": 1.0}
    await strat._maybe_enter(now, bar)
    handled = await strat._sync_and_manage_position(now, bar)

    assert handled is True
    assert super_enter.await_count == 1
    assert super_sync.await_count == 1


def test_template_option_plugin_generates_production_admission_config():
    plugin = TemplateOptionPlugin()
    cfg = build_option_strategy_instance_config(
        plugin=plugin,
        plugin_cls="strategies.options_universal.template_plugin:TemplateOptionPlugin",
        strategy_id="template_option_001",
        enabled=True,
        capital_limit_usd=1000,
    )

    assert cfg["params"]["extras"]["production_manifest"]["option_contract"]["required"] is True
    assert cfg["params"]["extras"]["option_manifest"]["trigger_source_ids"] == ["template_signal"]
    assert validate_option_strategy_config(cfg) == []


def test_option_manifest_validation_blocks_missing_contract_admission_fields():
    cfg = build_option_strategy_instance_config(
        plugin=_BadManifestPlugin(),
        plugin_cls="tests.unit.strategies.options_universal.test_universal_option_strategy:_BadManifestPlugin",
        strategy_id="bad_option_001",
    )

    blockers = validate_option_strategy_config(cfg)

    assert "bad_option_001:underlying_missing" in blockers
    assert "bad_option_001:trigger_source_ids_missing" in blockers
    assert "bad_option_001:allowed_rights_invalid" in blockers
    assert "bad_option_001:entry_cutoff_after_force_flatten" in blockers


def test_load_option_plugin_requires_option_plugin_type():
    plugin = load_option_plugin("strategies.options_universal.template_plugin:TemplateOptionPlugin")

    assert isinstance(plugin, TemplateOptionPlugin)
