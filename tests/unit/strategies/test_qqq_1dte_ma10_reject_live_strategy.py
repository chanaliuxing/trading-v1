import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from libs.strategy import StrategyParams
from strategies.qqq_1dte_ma10_reject_live_strategy import QQQ1DTEMA10RejectLiveStrategy


ET = ZoneInfo("America/New_York")


class _DummyLogger:
    def __init__(self):
        self.warning = MagicMock()
        self.info = MagicMock()
        self.error = MagicMock()
        self.debug = MagicMock()


def _make_strategy(extras=None):
    ctx = MagicMock()
    ctx.log = _DummyLogger()
    ctx.engine = MagicMock()
    ctx.subscribe_features = AsyncMock()
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    ctx.submit_intent = AsyncMock()
    ctx.portfolio = AsyncMock(return_value={"positions": []})
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.strategy_id = "qqq_1dte_ma10_reject_0324_001"
    params = StrategyParams()
    params.extras = extras or {}
    strat = QQQ1DTEMA10RejectLiveStrategy(ctx, params)
    return strat, ctx


@pytest.mark.asyncio
async def test_select_quote_prefers_1dte_over_2dte(monkeypatch):
    strat, _ctx = _make_strategy({"entry_quote_lookback_min": 2, "entry_quote_max_age_sec": 90})
    await strat.initialize()

    now = datetime(2026, 3, 25, 10, 0, 0, tzinfo=ET)

    class _FakeCH:
        def query(self, _sql, parameters=None):
            return [
                {
                    "ts": now,
                    "created_at": now,
                    "expiry": "2026-03-27",
                    "strike": 530.0,
                    "right": "C",
                    "dte": 2,
                    "bid": 2.20,
                    "ask": 2.32,
                    "mid": 2.26,
                    "iv": 0.25,
                    "delta": 0.50,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                    "open_interest": 500,
                },
                {
                    "ts": now,
                    "created_at": now,
                    "expiry": "2026-03-26",
                    "strike": 530.0,
                    "right": "C",
                    "dte": 1,
                    "bid": 2.05,
                    "ask": 2.15,
                    "mid": 2.10,
                    "iv": 0.24,
                    "delta": 0.49,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                    "open_interest": 800,
                },
            ]

    monkeypatch.setattr("strategies.ma_1dte_option_live_base.get_client", lambda: _FakeCH())
    quote, reason = strat._select_0dte_quote(now, side="CALL", spot=530.2, spread_max_pct=0.12)
    assert reason is None
    assert quote is not None
    assert "20260326" in quote["opt_symbol"]


@pytest.mark.asyncio
async def test_replay_signal_uses_two_bar_confirm_window(monkeypatch):
    strat, _ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()

    monkeypatch.setattr(strat, "_update_quote_data_health_gate", lambda now: None)
    monkeypatch.setattr(strat, "_maybe_release_quote_data_health_gate", lambda now: None)
    monkeypatch.setattr(strat, "_quote_heartbeat_allows_entry", lambda now: True)
    strat._submit_entry = AsyncMock()

    start = datetime(2026, 3, 25, 9, 30, 0, tzinfo=ET)
    for i in range(220):
        close = 500.0 + (i * 0.2)
        bar = {
            "ts": start + timedelta(minutes=i),
            "open": close - 0.05,
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "vwap": close - 0.10,
            "volume": 1000.0,
            "atr_d": 0.0,
            "atr_1m": 0.0,
            "atr_1m_ma": 0.0,
            "vol_ma": None,
            "rvol": None,
        }
        strat._bars.append(bar)

    setup_bar = strat._bars[-1]
    setup_bar["low"] = setup_bar["close"] - 1.10
    setup_bar["open"] = setup_bar["close"] - 0.20
    setup_bar["high"] = setup_bar["close"] + 0.15
    setup_bar["volume"] = 5000.0

    await strat._maybe_enter(setup_bar["ts"], setup_bar, wall_now=setup_bar["ts"])
    assert strat._pending_replay_setup is not None
    strat._submit_entry.assert_not_awaited()

    confirm_close = float(setup_bar["close"]) + 0.30
    confirm_bar = {
        "ts": setup_bar["ts"] + timedelta(minutes=1),
        "open": confirm_close - 0.10,
        "high": float(setup_bar["high"]) + 0.20,
        "low": float(setup_bar["low"]) + 0.10,
        "close": confirm_close,
        "vwap": confirm_close - 0.10,
        "volume": 2200.0,
        "atr_d": 0.0,
        "atr_1m": 0.0,
        "atr_1m_ma": 0.0,
        "vol_ma": None,
        "rvol": None,
    }
    strat._bars.append(confirm_bar)

    await strat._maybe_enter(confirm_bar["ts"], confirm_bar, wall_now=confirm_bar["ts"])
    strat._submit_entry.assert_awaited_once()


@pytest.mark.asyncio
async def test_quote_selection_reports_real_quote_age(monkeypatch):
    strat, _ctx = _make_strategy({"entry_quote_lookback_min": 2, "entry_quote_max_age_sec": 90})
    await strat.initialize()

    now = datetime(2026, 3, 25, 10, 0, 30, tzinfo=ET)

    class _FakeCH:
        def query(self, _sql, parameters=None):
            return [
                {
                    "ts": now - timedelta(seconds=30),
                    "created_at": now - timedelta(seconds=30),
                    "expiry": "2026-03-26",
                    "strike": 530.0,
                    "right": "C",
                    "dte": 1,
                    "bid": 2.05,
                    "ask": 2.15,
                    "mid": 2.10,
                    "iv": 0.24,
                    "delta": 0.49,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                    "open_interest": 800,
                },
            ]

    monkeypatch.setattr("strategies.ma_1dte_option_live_base.get_client", lambda: _FakeCH())
    quote, reason = strat._select_0dte_quote(now, side="CALL", spot=530.2, spread_max_pct=0.12)
    assert reason is None
    assert quote is not None
    assert quote["quote_age_sec"] == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_runtime_state_restores_structure_metadata(tmp_path: Path):
    strat, _ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    strat._state_path = tmp_path / "qqq_1dte_state.json"
    strat._day = datetime.now(ET).strftime("%Y-%m-%d")

    strat._pos = MagicMock()
    strat._pos.opt_symbol = "OPT:QQQ:20260326:C:530"
    strat._pos.side = "CALL"
    strat._pos.entry_time = datetime.now(ET)
    strat._pos.position_id = "pos:qqq:s1"
    strat._pos.entry_decision_id = "d-1"
    strat._pos.entry_trigger_id = "t-1"
    strat._pos.entry_snapshot_id = "s-1"
    strat._pos.entry_underlying = 530.25
    strat._pos.entry_close = 530.25
    strat._pos.entry_vwap = 529.90
    strat._pos.trace_id = "trace-1"
    strat._pos.tp1_done = False
    strat._pos.partial_taken = False
    strat._pos.filled = True
    strat._pos.signal_kind = "daily_best_additive"
    strat._pos.structure_stop = 528.75
    strat._pos.target_1r = 531.75
    strat._pos.target_1p2r = 532.05
    strat._pos.r_per_share = 1.5
    strat._pos.initial_contracts = 4
    strat._pos.best_fav_move_r = 0.6
    strat._pos.best_fav_hold_bars = 2
    strat._trades_filled_today = 1
    strat._first_trade_exit_reason = "TP1_1R"
    strat._persist_runtime_state(reason="test")

    strat._pos = None
    strat._trades_filled_today = 0
    strat._first_trade_exit_reason = None
    strat._restore_runtime_state()

    assert strat._pos is not None
    assert strat._pos.opt_symbol == "OPT:QQQ:20260326:C:530"
    assert strat._pos.position_id == "pos:qqq:s1"
    assert strat._pos.entry_decision_id == "d-1"
    assert strat._pos.entry_trigger_id == "t-1"
    assert strat._pos.entry_snapshot_id == "s-1"
    assert strat._pos.structure_stop == pytest.approx(528.75)
    assert strat._pos.target_1r == pytest.approx(531.75)
    assert strat._pos.r_per_share == pytest.approx(1.5)
    assert strat._trades_filled_today == 1
    assert strat._first_trade_exit_reason == "TP1_1R"


@pytest.mark.asyncio
async def test_sync_position_does_not_pause_on_foreign_option_open_order():
    strat, ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    ctx.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="OPT:QQQ:20260326:530:C",
                strategy_id="qqq_0dte_opt_live_001",
                order_ref="entry:qqq_0dte_opt_live_001:trace-1",
                status="Submitted",
            )
        ]
    )

    paused = await strat._sync_and_manage_position(datetime(2026, 3, 25, 10, 0, tzinfo=ET), {"close": 530.0, "vwap": 529.9})

    assert paused is False


@pytest.mark.asyncio
async def test_sync_position_keeps_pending_entry_only_for_strategy_owned_order():
    strat, ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    strat._pos = MagicMock(opt_symbol="OPT:QQQ:20260326:530:C", filled=False, last_exit_reason=None)
    ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="OPT:QQQ:20260326:530:C",
                strategy_id="qqq_0dte_opt_live_001",
                order_ref="entry:qqq_0dte_opt_live_001:trace-1",
                status="Submitted",
            )
        ]
    )

    paused = await strat._sync_and_manage_position(datetime(2026, 3, 25, 10, 1, tzinfo=ET), {"close": 530.0, "vwap": 529.9})

    assert paused is False
    assert strat._pos is None


@pytest.mark.asyncio
async def test_sync_position_caps_full_exit_to_strategy_owned_contracts():
    strat, ctx = _make_strategy({"preload_history_bars": 0, "contracts": 4, "force_flatten_time": "15:50:00"})
    await strat.initialize()
    now = datetime(2026, 3, 25, 15, 50, 0, tzinfo=ET)
    strat._pos = MagicMock(
        opt_symbol="OPT:QQQ:20260326:530:C",
        side="CALL",
        entry_time=now,
        entry_underlying=530.0,
        trace_id="trace-1",
        filled=True,
        partial_taken=False,
        tp1_done=False,
        last_exit_reason=None,
        structure_stop=528.0,
        target_1r=532.0,
        target_1p2r=532.4,
        r_per_share=2.0,
        initial_contracts=4,
    )
    ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=8.0, avg_px=2.0))
    strat._submit_exit = AsyncMock(return_value=4)

    handled = await strat._sync_and_manage_position(now, {"close": 530.5, "vwap": 530.2, "high": 531.0, "low": 529.0})

    assert handled is True
    strat._submit_exit.assert_awaited_once()
    assert strat._submit_exit.await_args.kwargs["sell_qty"] == 4
    assert strat._submit_exit.await_args.kwargs["current_qty"] == 4.0


@pytest.mark.asyncio
async def test_submit_entry_propagates_same_symbol_and_shared_cap_extras(monkeypatch):
    strat, ctx = _make_strategy(
        {
            "allow_same_option_symbol_cross_strategy": True,
            "shared_cap_group_id": "QQQ_OPT_LIVE_SHARED",
            "shared_cap_group_limit_usd": 20000,
            "shared_cap_group_strategy_ids": ["qqq_0dte_opt_live_001", "qqq_1dte_ma10_reject_0324_001"],
        }
    )
    await strat.initialize()

    now = datetime(2026, 3, 25, 10, 0, 0, tzinfo=ET)

    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260326:C:530",
                "strike": 530.0,
                "bid": 2.0,
                "ask": 2.1,
                "mid": 2.05,
                "last": 2.05,
                "quote_age_sec": 12.0,
            },
            None,
        ),
    )

    await strat._submit_entry(
        now,
        side="CALL",
        spot=530.2,
        entry_bar={"vwap": 530.0, "atr_1m": 0.8},
        spread_max_pct=0.12,
        wall_now=now,
    )

    env = ctx.submit_intent.await_args.args[0]
    assert env.extras["allow_same_option_symbol_cross_strategy"] is True
    assert env.extras["shared_cap_group_id"] == "QQQ_OPT_LIVE_SHARED"
    assert env.extras["shared_cap_group_limit_usd"] == 20000
    assert env.extras["shared_cap_group_strategy_ids"] == ["qqq_0dte_opt_live_001", "qqq_1dte_ma10_reject_0324_001"]
    assert env.extras["allocation_id"] == env.extras["trace_id"]


@pytest.mark.asyncio
async def test_submit_entry_writes_decision_snapshot_when_enabled(tmp_path: Path, monkeypatch):
    strat, ctx = _make_strategy(
        {
            "enable_live_parity_snapshot": True,
            "live_parity_snapshot_root": str(tmp_path),
        }
    )
    await strat.initialize()
    now = datetime(2026, 3, 25, 11, 11, 0, tzinfo=ET)

    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260326:P:588",
                "strike": 588.0,
                "bid": 3.90,
                "ask": 4.02,
                "mid": 3.96,
                "last": 3.98,
                "quote_age_sec": 12.0,
            },
            None,
        ),
    )
    strat._pending_entry_plan = {
        "signal_kind": "trend_continuation_put",
        "structure_stop": 589.8,
        "target_1r": 587.9,
        "target_1p2r": 587.5,
        "r_per_share": 0.8,
        "initial_contracts": 4,
    }

    await strat._submit_entry(
        now,
        side="PUT",
        spot=588.62,
        entry_bar={"vwap": 589.10, "atr_1m": 0.62},
        spread_max_pct=0.12,
        wall_now=now,
    )

    env = ctx.submit_intent.await_args.args[0]
    snapshot_id = env.extras["decision_snapshot_id"]
    assert snapshot_id
    snapshot_path = tmp_path / str(strat.strategy_id) / "2026-03-25" / f"{snapshot_id}.json"
    assert snapshot_path.exists()
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["parity_profile_id"] == "qqq_option_ma_ext_v1"
    assert payload["signal"]["signal_kind"] == "trend_continuation_put"
    assert payload["selected_contract"]["opt_symbol"] == "OPT:QQQ:20260326:P:588"
    assert env.extras["decision_snapshot_key"]
    assert env.extras["parity_profile_id"] == "qqq_option_ma_ext_v1"
    assert env.extras["position_id"].startswith("pos:qqq_1dte_ma10_reject_0324_001:")


@pytest.mark.asyncio
async def test_submit_entry_shadow_mode_writes_snapshot_without_submit(tmp_path: Path, monkeypatch):
    strat, ctx = _make_strategy(
        {
            "enable_live_parity_snapshot": True,
            "enable_live_shadow_mode": True,
            "live_parity_snapshot_root": str(tmp_path),
        }
    )
    await strat.initialize()
    now = datetime(2026, 3, 25, 11, 11, 0, tzinfo=ET)

    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260326:P:588",
                "strike": 588.0,
                "bid": 3.90,
                "ask": 4.02,
                "mid": 3.96,
                "last": 3.98,
                "quote_age_sec": 12.0,
            },
            None,
        ),
    )
    strat._pending_entry_plan = {"signal_kind": "trend_continuation_put"}

    await strat._submit_entry(
        now,
        side="PUT",
        spot=588.62,
        entry_bar={"vwap": 589.10, "atr_1m": 0.62},
        spread_max_pct=0.12,
        wall_now=now,
    )

    ctx.submit_intent.assert_not_awaited()
    assert strat._pos is None
    snapshot_files = list((tmp_path / str(strat.strategy_id) / "2026-03-25").glob("*.json"))
    assert len(snapshot_files) == 1


@pytest.mark.asyncio
async def test_submit_entry_duplicate_snapshot_blocks_second_submit(tmp_path: Path, monkeypatch):
    strat, ctx = _make_strategy(
        {
            "enable_live_parity_snapshot": True,
            "live_parity_snapshot_root": str(tmp_path),
        }
    )
    await strat.initialize()
    now = datetime(2026, 3, 25, 11, 11, 0, tzinfo=ET)

    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260326:P:588",
                "strike": 588.0,
                "bid": 3.90,
                "ask": 4.02,
                "mid": 3.96,
                "last": 3.98,
                "quote_age_sec": 12.0,
            },
            None,
        ),
    )
    strat._pending_entry_plan = {"signal_kind": "trend_continuation_put"}

    await strat._submit_entry(
        now,
        side="PUT",
        spot=588.62,
        entry_bar={"vwap": 589.10, "atr_1m": 0.62},
        spread_max_pct=0.12,
        wall_now=now,
    )
    first_calls = ctx.submit_intent.await_count
    strat._pos = None
    strat._pending_entry_plan = {"signal_kind": "trend_continuation_put"}

    await strat._submit_entry(
        now,
        side="PUT",
        spot=588.62,
        entry_bar={"vwap": 589.10, "atr_1m": 0.62},
        spread_max_pct=0.12,
        wall_now=now,
    )

    assert ctx.submit_intent.await_count == first_calls


@pytest.mark.asyncio
async def test_sync_and_manage_adopted_position_without_metadata_does_not_submit_intraday_exit(monkeypatch):
    strat, ctx = _make_strategy({"preload_history_bars": 0, "contracts": 4})
    await strat.initialize()

    now = datetime(2026, 3, 25, 11, 0, 0, tzinfo=ET)
    bar = {
        "ts": now,
        "open": 530.0,
        "high": 531.0,
        "low": 529.0,
        "close": 530.5,
        "vwap": 530.2,
        "volume": 1200.0,
    }
    strat._pos = MagicMock(
        opt_symbol="OPT:QQQ:20260326:530:C",
        side="CALL",
        entry_time=now,
        entry_underlying=530.5,
        entry_close=530.5,
        entry_vwap=530.2,
        trace_id="adopted",
        tp1_done=False,
        partial_taken=False,
        filled=True,
        last_exit_reason=None,
        structure_stop=0.0,
        target_1r=0.0,
        target_1p2r=0.0,
        r_per_share=0.0,
        initial_contracts=4,
    )
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=4.0, avg_px=2.0))
    strat._submit_exit = AsyncMock()
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *_args, **_kwargs: 2.15)
    monkeypatch.setattr(strat, "_build_feature_frame", lambda: pd.DataFrame([{"ma10": 529.8}]))

    handled = await strat._sync_and_manage_position(now, bar)

    assert handled is True
    assert strat._pos is not None
    assert strat._pos.trace_id == "adopted"
    assert float(getattr(strat._pos, "structure_stop", 0.0) or 0.0) == 0.0
    strat._submit_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_and_manage_adopted_position_without_metadata_only_eod_flattens(monkeypatch):
    strat, ctx = _make_strategy({"preload_history_bars": 0, "contracts": 4, "force_flatten_time": "15:50:00"})
    await strat.initialize()

    now = datetime(2026, 3, 25, 15, 50, 0, tzinfo=ET)
    bar = {
        "ts": now,
        "open": 530.0,
        "high": 531.0,
        "low": 529.0,
        "close": 530.5,
        "vwap": 530.2,
        "volume": 1200.0,
    }
    strat._pos = MagicMock(
        opt_symbol="OPT:QQQ:20260326:530:C",
        side="CALL",
        entry_time=now,
        entry_underlying=530.5,
        entry_close=530.5,
        entry_vwap=530.2,
        trace_id="adopted",
        tp1_done=False,
        partial_taken=False,
        filled=True,
        last_exit_reason=None,
        structure_stop=0.0,
        target_1r=0.0,
        target_1p2r=0.0,
        r_per_share=0.0,
        initial_contracts=4,
    )
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=4.0, avg_px=2.0))
    strat._submit_exit = AsyncMock(return_value=4)
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *_args, **_kwargs: 2.15)
    monkeypatch.setattr(strat, "_build_feature_frame", lambda: pd.DataFrame([{"ma10": 529.8}]))

    handled = await strat._sync_and_manage_position(now, bar)

    assert handled is True
    strat._submit_exit.assert_awaited_once()
    call = strat._submit_exit.await_args
    assert call.args[1] == "OPT:QQQ:20260326:530:C"
    assert call.kwargs["reason"] == "EOD_FORCE_FLATTEN"
    assert call.kwargs["sell_qty"] == 4
