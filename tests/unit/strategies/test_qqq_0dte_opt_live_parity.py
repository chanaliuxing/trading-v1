from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyParams
from strategies.options_kernel.parity import OptionParityRunner
from strategies.qqq_0dte_opt_live_strategy import QQQ0DTEOptionsLiveStrategy
from strategies.qqq_0dte_opt_live_v2_strategy import QQQ0DTEOptionsLiveV2Strategy


ET = ZoneInfo("America/New_York")


class _DummyLogger:
    def __init__(self):
        self.warning = MagicMock()
        self.info = MagicMock()
        self.error = MagicMock()
        self.debug = MagicMock()


def _make_ctx(strategy_id: str):
    ctx = MagicMock()
    ctx.log = _DummyLogger()
    ctx.engine = MagicMock()
    ctx.system_health = MagicMock()
    ctx.system_health.subscribe = AsyncMock()
    ctx.system_health.unsubscribe = AsyncMock()
    ctx.subscribe_features = AsyncMock()
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    ctx.portfolio = AsyncMock(return_value={"positions": []})
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    ctx.strategy_id = strategy_id
    return ctx


@pytest.mark.asyncio
async def test_qqq_0dte_v2_matches_legacy_entry_intent_shape(tmp_path: Path, monkeypatch):
    params_old = StrategyParams(extras={"preload_history_bars": 0})
    ctx_old = _make_ctx("qqq_0dte_opt_live_legacy_test")
    old = QQQ0DTEOptionsLiveStrategy(ctx_old, params_old)
    await old.initialize()

    params_v2 = StrategyParams(
        extras={
            "preload_history_bars": 0,
            "live_parity_snapshot_root": str(tmp_path / "snapshots"),
        }
    )
    ctx_v2 = _make_ctx("qqq_0dte_opt_live_v2_test")
    new = QQQ0DTEOptionsLiveV2Strategy(ctx_v2, params_v2)
    await new.initialize()

    now = datetime(2026, 3, 25, 10, 12, 0, tzinfo=ET)
    quote_payload = (
        {
            "opt_symbol": "OPT:QQQ:20260325:C:530",
            "strike": 530.0,
            "bid": 1.90,
            "ask": 2.02,
            "mid": 1.96,
            "last": 1.98,
            "quote_age_sec": 12.0,
        },
        None,
    )
    monkeypatch.setattr(old, "_select_0dte_quote", lambda *args, **kwargs: quote_payload)
    monkeypatch.setattr(new.kernel.delegate, "_select_0dte_quote", lambda *args, **kwargs: quote_payload)

    await old._submit_entry(
        now,
        side="CALL",
        spot=530.62,
        entry_bar={"vwap": 530.10, "atr_1m": 0.62},
        spread_max_pct=0.15,
        wall_now=now,
    )
    await new.kernel.delegate._submit_entry(
        now,
        side="CALL",
        spot=530.62,
        entry_bar={"vwap": 530.10, "atr_1m": 0.62},
        spread_max_pct=0.15,
        wall_now=now,
    )

    old_env = ctx_old.submit_intent.await_args.args[0]
    new_env = ctx_v2.submit_intent.await_args.args[0]
    result = OptionParityRunner().compare_envelopes([old_env], [new_env])
    assert result["matched"] is True
    assert new.kernel.state_snapshot()["ledger"]["lots"]


def test_qqq_0dte_tradability_distinguishes_idle_from_entry_ready(monkeypatch):
    params = StrategyParams(
        extras={
            "preload_history_bars": 0,
            "min_warmup_bars": 2,
            "tradability_min_warmup_bars": 2,
            "quote_hb_max_age_sec": 120.0,
        }
    )
    strategy = QQQ0DTEOptionsLiveStrategy(_make_ctx("qqq_0dte_tradability_test"), params)
    strategy._pending = None
    strategy._pos = None
    strategy._trades_filled_today = 0
    strategy._quote_health_block_day = False
    strategy._quote_hb_pause_new_entries = False
    strategy._bars = []
    strategy._bars.extend([{"close": 1.0}, {"close": 1.1}])
    monkeypatch.setattr(strategy, "_latest_quote_status", lambda now: {"source": "memory", "age_sec": 999999.0})

    blockers, details = strategy._tradability_blockers(datetime(2026, 5, 5, 18, 30, tzinfo=ET))

    assert blockers == []
    assert details["readiness_state"] == "idle_not_in_entry_window"
    assert details["idle_ok"] is True
    assert details["tradable_for_entry"] is False
    assert details["data_health_ok"] is False
    assert details["data_blockers"] == ["quote_stale"]


def test_qqq_0dte_idle_heartbeat_is_not_top_level_tradable(monkeypatch):
    captured = {}
    params = StrategyParams(
        extras={
            "preload_history_bars": 0,
            "min_warmup_bars": 2,
            "tradability_min_warmup_bars": 2,
            "quote_hb_max_age_sec": 120.0,
        }
    )
    strategy = QQQ0DTEOptionsLiveStrategy(_make_ctx("qqq_0dte_idle_heartbeat_test"), params)
    strategy.underlying = "QQQ"
    strategy._pending = None
    strategy._pos = None
    strategy._trades_filled_today = 0
    strategy._quote_health_block_day = False
    strategy._quote_hb_pause_new_entries = False
    strategy._bars = [{"close": 1.0}, {"close": 1.1}]
    monkeypatch.setattr(strategy, "_latest_quote_status", lambda now: {"source": "memory", "age_sec": 999999.0})
    monkeypatch.setattr(
        "strategies.qqq_0dte_opt_live_strategy.write_strategy_tradability_heartbeat",
        lambda **kwargs: captured.update(kwargs),
    )

    blockers, details = strategy._emit_tradability_heartbeat(
        datetime(2026, 5, 5, 18, 30, tzinfo=ET),
        source="option_idle_heartbeat",
    )

    assert blockers == []
    assert details["readiness_state"] == "idle_not_in_entry_window"
    assert captured["tradable"] is False
    assert captured["blockers"] == ["idle_not_in_entry_window"]
    assert captured["details"]["tradable_for_entry"] is False
    assert captured["details"]["data_health_ok"] is False
    assert captured["details"]["tradable"] is False


def test_qqq_0dte_tradability_blocks_entry_window_on_stale_quote(monkeypatch):
    params = StrategyParams(
        extras={
            "preload_history_bars": 0,
            "min_warmup_bars": 1,
            "tradability_min_warmup_bars": 1,
            "quote_hb_max_age_sec": 120.0,
        }
    )
    strategy = QQQ0DTEOptionsLiveStrategy(_make_ctx("qqq_0dte_tradability_entry_test"), params)
    strategy._pending = None
    strategy._pos = None
    strategy._trades_filled_today = 0
    strategy._quote_health_block_day = False
    strategy._quote_hb_pause_new_entries = False
    strategy._bars = []
    strategy._bars.append({"close": 1.0})
    monkeypatch.setattr(strategy, "_latest_quote_status", lambda now: {"source": "memory", "age_sec": 999999.0})

    blockers, details = strategy._tradability_blockers(datetime(2026, 5, 5, 10, 30, tzinfo=ET))

    assert blockers == ["quote_stale"]
    assert details["readiness_state"] == "entry_blocked"
    assert details["tradable_for_entry"] is False
    assert details["data_health_ok"] is False
