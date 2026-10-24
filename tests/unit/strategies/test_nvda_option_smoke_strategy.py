from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyParams
from strategies.nvda_option_smoke_strategy import (
    NVDAOptionChurnSmokePlugin,
    NVDAOptionChurnSmokeStrategy,
    NVDAStockSignalOptionPlugin,
)
from strategies.options_universal import OptionBarContext, OptionQuoteRequest


ET = ZoneInfo("America/New_York")


def _strategy(plugin_extras: dict | None = None) -> NVDAOptionChurnSmokeStrategy:
    ctx = MagicMock()
    ctx.strategy_id = "nvda_option_smoke_test"
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.engine = MagicMock()
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=1.0))
    params = StrategyParams(extras=dict(plugin_extras or {}))
    strat = NVDAOptionChurnSmokeStrategy(ctx, params)
    strat.underlying = "NVDA"
    strat.risk_symbol = "OPT:NVDA:SMOKE"
    strat._pos = None
    strat._make_audit_ids = MagicMock(return_value=("trigger-1", "decision-1", "trace-1"))
    strat._emit_local_audit = MagicMock()
    strat._submit_entry = AsyncMock()
    strat._submit_exit = AsyncMock(return_value=1)
    return strat


def test_nvda_smoke_defaults_are_paper_sized() -> None:
    extras: dict = {}
    NVDAOptionChurnSmokePlugin().apply_default_extras(extras)

    assert extras["underlying"] == "NVDA"
    assert extras["contracts"] == 1
    assert extras["max_trades_per_day"] == 999
    assert extras["paper_smoke_only"] is True
    assert extras["dte_min"] == 0
    assert extras["dte_max"] == 7


@pytest.mark.asyncio
async def test_churn_plugin_submits_entry_when_idle() -> None:
    strat = _strategy({"smoke_entry_cadence_minutes": 1, "allow_live_smoke_entry": True})
    plugin = NVDAOptionChurnSmokePlugin()
    now = datetime(2026, 5, 5, 10, 0, tzinfo=ET)

    handled = await plugin.maybe_enter(
        strat,
        OptionBarContext(now=now, bar={"close": 900.0, "vwap": 899.0}, wall_now=now),
    )

    assert handled is True
    strat._submit_entry.assert_awaited_once()
    assert strat._submit_entry.await_args.kwargs["side"] in {"CALL", "PUT"}


@pytest.mark.asyncio
async def test_stock_signal_plugin_buys_call_on_nvda_up_signal() -> None:
    strat = _strategy({"stock_signal_move_bps": 4.0, "allow_live_smoke_entry": True})
    strat._bars = [
        {"ts": datetime(2026, 5, 5, 9, 59, tzinfo=ET), "close": 900.0, "vwap": 899.0},
        {"ts": datetime(2026, 5, 5, 10, 0, tzinfo=ET), "close": 901.0, "vwap": 900.0},
    ]
    plugin = NVDAStockSignalOptionPlugin()
    now = datetime(2026, 5, 5, 10, 0, tzinfo=ET)

    handled = await plugin.maybe_enter(
        strat,
        OptionBarContext(now=now, bar=strat._bars[-1], wall_now=now),
    )

    assert handled is True
    strat._submit_entry.assert_awaited_once()
    assert strat._submit_entry.await_args.kwargs["side"] == "CALL"


@pytest.mark.asyncio
async def test_churn_plugin_blocks_live_paper_smoke_entry_by_default(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    strat = _strategy({"smoke_entry_cadence_minutes": 1, "paper_smoke_only": True})
    plugin = NVDAOptionChurnSmokePlugin()
    now = datetime(2026, 5, 5, 10, 0, tzinfo=ET)

    handled = await plugin.maybe_enter(
        strat,
        OptionBarContext(now=now, bar={"close": 900.0, "vwap": 899.0}, wall_now=now),
    )

    assert handled is True
    strat._submit_entry.assert_not_awaited()
    strat._emit_local_audit.assert_called()
    assert strat._emit_local_audit.call_args.kwargs["reason"] == "live_smoke_entry_disabled"


def test_paper_smoke_does_not_allow_degraded_entry_by_default() -> None:
    strat = _strategy({"paper_smoke_only": True})

    _proceed, extras = strat._prepare_entry_intent(
        now=datetime(2026, 5, 5, 10, 0, tzinfo=ET),
        side="CALL",
        spot=900.0,
        entry_bar={"close": 900.0},
        quote={"opt_symbol": "OPT:NVDA:20260508:900:C", "mid": 1.0},
        trigger_id="trigger-1",
        decision_id="decision-1",
        trace_id="trace-1",
    )

    assert extras["paper_smoke_only"] is True
    assert "allow_system_health_degraded_entry" not in extras


@pytest.mark.asyncio
async def test_smoke_plugin_exits_after_hold_seconds() -> None:
    strat = _strategy({"smoke_hold_seconds": 30})
    now = datetime(2026, 5, 5, 10, 1, tzinfo=ET)
    strat._pos = SimpleNamespace(
        opt_symbol="OPT:NVDA:20260508:900:C",
        entry_time=now - timedelta(seconds=31),
        side="CALL",
        trace_id="trace-1",
    )
    plugin = NVDAOptionChurnSmokePlugin()

    handled = await plugin.sync_and_manage_position(strat, OptionBarContext(now=now, bar={"close": 901.0}))

    assert handled is True
    strat._submit_exit.assert_awaited_once_with(
        now,
        "OPT:NVDA:20260508:900:C",
        sell_qty=1,
        reason="SMOKE_CYCLE_EXIT",
        current_qty=1.0,
    )


def test_select_quote_uses_configurable_nvda_dte_range(monkeypatch) -> None:
    strat = _strategy({"dte_min": 0, "dte_max": 7, "dte_preferred": 3})
    now = datetime(2026, 5, 5, 10, 0, tzinfo=ET)

    def fake_query_option_rows(**kwargs):
        assert kwargs["underlying"] == "NVDA"
        assert kwargs["dte_min"] == 0
        assert kwargs["dte_max"] == 7
        return [
            {
                "ts": now,
                "created_at": now,
                "expiry": date(2026, 5, 8),
                "strike": 905.0,
                "right": "C",
                "dte": 3,
                "bid": 4.9,
                "ask": 5.1,
                "mid": 5.0,
                "quote_source": "memory",
            }
        ]

    monkeypatch.setattr("strategies.nvda_option_smoke_strategy.query_option_rows", fake_query_option_rows)

    selected = NVDAOptionChurnSmokePlugin().select_quote(
        strat,
        OptionQuoteRequest(now=now, side="CALL", spot=902.0, spread_max_pct=0.20, now_for_quote_age=now),
    )

    assert selected is not None
    assert selected.reject_reason is None
    assert selected.quote is not None
    assert selected.quote["opt_symbol"] == "OPT:NVDA:20260508:905:C"
