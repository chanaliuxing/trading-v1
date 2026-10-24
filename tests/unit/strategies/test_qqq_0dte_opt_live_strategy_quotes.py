from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from libs.options.internal_symbol import format_internal_opt_symbol
from libs.strategy import StrategyParams
from strategies.qqq_0dte_opt_live_strategy import QQQ0DTEOptionsLiveStrategy, _PositionState


ET = ZoneInfo("America/New_York")


class _DummyLogger:
    def __init__(self):
        self.warning = MagicMock()
        self.info = MagicMock()
        self.error = MagicMock()
        self.debug = MagicMock()


class _Pos:
    def __init__(self, qty: float, avg_px: float):
        self.pos_qty = qty
        self.avg_px = avg_px


class _OpenOrder:
    def __init__(
        self,
        *,
        symbol: str,
        side: str,
        status: str = "Submitted",
        remaining: float = 0.0,
        qty: float | None = None,
        order_id: int = 1,
        order_type: str = "LMT",
        order_ref: str = "",
    ):
        self.symbol = symbol
        self.side = side
        self.status = status
        self.remaining = remaining
        self.qty = qty if qty is not None else remaining
        self.order_id = order_id
        self.order_type = order_type
        self.order_ref = order_ref


def _make_strategy(extras=None):
    ctx = MagicMock()
    ctx.log = _DummyLogger()
    ctx.engine = MagicMock()
    ctx.subscribe_features = AsyncMock()
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    ctx.submit_intent = AsyncMock()
    ctx.strategy_id = "qqq_0dte_opt_live_001"
    params = StrategyParams()
    params.extras = extras or {}
    strat = QQQ0DTEOptionsLiveStrategy(ctx, params)
    return strat, ctx


def _patch_quote_clients(monkeypatch, factory) -> None:
    monkeypatch.setattr("services.market.options_quote_store.get_client", factory)


@pytest.mark.asyncio
async def test_select_quote_accepts_recent_previous_bucket(monkeypatch):
    strat, _ctx = _make_strategy(
        {
            "entry_quote_lookback_min": 2,
            "entry_quote_max_age_sec": 90,
            "entry_quote_allow_ch_fallback": True,
        }
    )
    await strat.initialize()

    now = datetime(2026, 3, 5, 10, 0, 0, tzinfo=ET)

    class _FakeCH:
        def query(self, _sql, parameters=None):
            return [
                {
                    "ts": now - timedelta(minutes=1),
                    "strike": 600.0,
                    "bid": 1.0,
                    "ask": 1.1,
                    "mid": 1.05,
                    "last": 1.05,
                    "created_at": now - timedelta(seconds=40),
                }
            ]

    _patch_quote_clients(monkeypatch, lambda: _FakeCH())

    q, reason = strat._select_0dte_quote(now, side="CALL", spot=600.2, spread_max_pct=0.2)
    assert reason is None
    assert q is not None
    assert q["mid"] == pytest.approx(1.05)
    assert q["quote_age_sec"] <= 90


@pytest.mark.asyncio
async def test_initialize_subscribes_5s_primary_by_default():
    strat, ctx = _make_strategy()
    await strat.initialize()
    ctx.subscribe_features.assert_awaited_once_with(["QQQ"], bar_sec=5)
    ctx.enable_realtime_feature_engine.assert_awaited_once()
    assert strat._data_mode == "rfe_pending"


@pytest.mark.asyncio
async def test_parse_ts_accepts_microseconds():
    strat, _ctx = _make_strategy()
    await strat.initialize()

    ts = int(datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET).timestamp() * 1_000_000)
    parsed = strat._parse_ts({"ts": ts})
    assert parsed == datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET)


@pytest.mark.asyncio
async def test_rfe_emit_promotes_strategy_to_rfe_primary(monkeypatch):
    strat, _ctx = _make_strategy()
    await strat.initialize()
    strat._process_completed_minute_bar = AsyncMock()

    ts_us = int(datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET).timestamp() * 1_000_000)
    await strat.on_features(
        "QQQ",
        {
            "ts": ts_us,
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "vwap_1m": 100.25,
            "volume": 1000.0,
        },
    )

    assert strat._data_mode == "rfe_primary"
    strat._process_completed_minute_bar.assert_awaited_once()


@pytest.mark.asyncio
async def test_falls_back_to_5s_when_rfe_grace_elapsed():
    strat, _ctx = _make_strategy({"rfe_primary_grace_sec": 0})
    await strat.initialize()

    ts = datetime(2026, 3, 13, 9, 30, 5, tzinfo=ET)
    await strat.on_features(
        "QQQ",
        {
            "ts": ts,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.1,
            "vwap": 100.0,
            "volume": 10.0,
        },
    )

    assert strat._data_mode == "bar5s_fallback"


@pytest.mark.asyncio
async def test_does_not_switch_back_to_rfe_after_bar5s_fallback():
    strat, _ctx = _make_strategy({"rfe_primary_grace_sec": 0})
    await strat.initialize()

    ts_5s = datetime(2026, 3, 13, 9, 30, 5, tzinfo=ET)
    await strat.on_features(
        "QQQ",
        {
            "ts": ts_5s,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.1,
            "vwap": 100.0,
            "volume": 10.0,
        },
    )
    assert strat._data_mode == "bar5s_fallback"

    strat._process_completed_minute_bar = AsyncMock()
    ts_us = int(datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET).timestamp() * 1_000_000)
    await strat.on_features(
        "QQQ",
        {
            "ts": ts_us,
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "vwap_1m": 100.25,
            "volume": 1000.0,
        },
    )

    assert strat._data_mode == "bar5s_fallback"
    strat._process_completed_minute_bar.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_subminute_bar_builds_completed_1m_bar():
    strat, _ctx = _make_strategy({})
    await strat.initialize()

    samples = [
        (datetime(2026, 3, 13, 9, 30, 5, tzinfo=ET), 100.0),
        (datetime(2026, 3, 13, 9, 30, 10, tzinfo=ET), 101.0),
        (datetime(2026, 3, 13, 9, 30, 55, tzinfo=ET), 102.0),
        (datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET), 103.0),
    ]
    for ts, close in samples:
        features = {
            "ts": ts,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.3,
            "close": close,
            "vwap": close,
            "volume": 10.0,
        }
        completed = strat._ingest_subminute_bar(ts, features)

    assert completed is None

    ts = datetime(2026, 3, 13, 9, 31, 5, tzinfo=ET)
    features = {
        "ts": ts,
        "open": 104.0,
        "high": 104.2,
        "low": 103.9,
        "close": 104.0,
        "vwap": 104.0,
        "volume": 10.0,
    }
    completed = strat._ingest_subminute_bar(ts, features)

    assert completed is not None
    minute_ts, bar = completed
    assert minute_ts == datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET)
    assert bar["open"] == pytest.approx(99.9)
    assert bar["high"] == pytest.approx(103.2)
    assert bar["low"] == pytest.approx(99.7)
    assert bar["close"] == pytest.approx(103.0)
    assert bar["volume"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_resolve_atr_d_falls_back_to_atr_1m_multiple():
    strat, _ctx = _make_strategy({"atr_d_mult_from_atr_1m": 40.0})
    await strat.initialize()

    atr_d = strat._resolve_atr_d({"atr_d": 0.0, "atr_1m": 0.25})
    assert atr_d == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_sync_position_ignores_foreign_strategy_open_option_order():
    strat, ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    ctx.open_orders = AsyncMock(
        return_value=[
            _OpenOrder(
                symbol="OPT:QQQ:20260326:530:C",
                side="BUY",
                status="Submitted",
                remaining=4,
                order_ref="entry:qqq_1dte_ma10_reject_0324_001:trace-1",
            )
        ]
    )

    paused = await strat._sync_and_manage_position(datetime(2026, 3, 25, 10, 0, tzinfo=ET), {"close": 530.0, "vwap": 529.9})

    assert paused is False
    assert strat._pos is None


@pytest.mark.asyncio
async def test_sync_position_does_not_adopt_heuristic_foreign_position(monkeypatch):
    strat, ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    ctx.portfolio = AsyncMock(
        return_value={
            "positions": [
                {"symbol": "OPT:QQQ:20260325:530:C", "pos_qty": 4.0},
            ]
        }
    )
    ctx.open_orders = AsyncMock(return_value=[])
    monkeypatch.setattr(
        strat,
        "_restore_adopted_position_state",
        lambda now, sym, qty: {"source": "heuristic_qty", "trace_id": "foreign", "tp1_done": False, "entry_time": now},
    )

    paused = await strat._sync_and_manage_position(datetime(2026, 3, 25, 10, 1, tzinfo=ET), {"close": 530.0, "vwap": 529.9})

    assert paused is False
    assert strat._pos is None


@pytest.mark.asyncio
async def test_working_exit_state_ignores_foreign_strategy_sell_order():
    strat, ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    opt_symbol = "OPT:QQQ:20260325:530:C"
    ctx.open_orders = AsyncMock(
        return_value=[
            _OpenOrder(
                symbol=opt_symbol,
                side="SELL",
                status="Submitted",
                remaining=2,
                order_ref="exit:qqq_1dte_ma10_reject_0324_001:trace-1",
            ),
            _OpenOrder(
                symbol=opt_symbol,
                side="SELL",
                status="Submitted",
                remaining=1,
                order_ref="exit:qqq_0dte_opt_live_001:trace-2",
            ),
        ]
    )

    working_qty, working_sample = await strat._get_working_exit_state(opt_symbol)

    assert working_qty == 1
    assert len(working_sample) == 1
    assert working_sample[0]["order_ref"] == "exit:qqq_0dte_opt_live_001:trace-2"


@pytest.mark.asyncio
async def test_submit_exit_caps_qty_to_strategy_owned_contracts():
    strat, ctx = _make_strategy({"preload_history_bars": 0})
    await strat.initialize()
    strat._pos = _PositionState(
        opt_symbol="OPT:QQQ:20260325:530:C",
        side="CALL",
        entry_time=datetime(2026, 3, 25, 10, 0, tzinfo=ET),
        entry_underlying=530.0,
        trace_id="trace-1",
    )
    strat._pos.filled = True
    strat._pos.initial_contracts = 4
    ctx.open_orders = AsyncMock(return_value=[])
    strat._get_quote_for_contract = MagicMock(return_value={"bid": 1.0, "ask": 1.1, "mid": 1.05, "age_sec": 1.0})
    ctx.submit_intent = AsyncMock()

    submitted = await strat._submit_exit(
        datetime(2026, 3, 25, 10, 5, tzinfo=ET),
        "OPT:QQQ:20260325:530:C",
        sell_qty=8,
        reason="TEST_EXIT",
        current_qty=8.0,
    )

    assert submitted == 4


@pytest.mark.asyncio
async def test_process_completed_minute_bar_dedupes_same_minute():
    strat, _ctx = _make_strategy({"rfe_primary_enabled": False})
    await strat.initialize()
    strat._sync_and_manage_position = AsyncMock(return_value=False)
    strat._maybe_enter = AsyncMock()

    now = datetime(2026, 3, 13, 9, 31, 0, tzinfo=ET)
    bar = {
        "ts": now,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "vwap": 100.25,
        "volume": 100.0,
        "atr_d": 0.0,
        "atr_1m": 0.0,
        "atr_1m_ma": 0.0,
        "vol_ma": None,
        "rvol": None,
    }

    await strat._process_completed_minute_bar(now, dict(bar))
    await strat._process_completed_minute_bar(now, dict(bar))

    assert len(strat._bars) == 1
    strat._maybe_enter.assert_awaited_once()


@pytest.mark.asyncio
async def test_select_quote_rejects_stale(monkeypatch):
    strat, _ctx = _make_strategy(
        {
            "entry_quote_lookback_min": 2,
            "entry_quote_max_age_sec": 30,
            "entry_quote_allow_ch_fallback": True,
        }
    )
    await strat.initialize()

    now = datetime(2026, 3, 5, 10, 0, 0, tzinfo=ET)

    class _FakeCH:
        def query(self, _sql, parameters=None):
            return [
                {
                    "ts": now - timedelta(minutes=1),
                    "strike": 600.0,
                    "bid": 1.0,
                    "ask": 1.1,
                    "mid": 1.05,
                    "last": 1.05,
                    "created_at": now - timedelta(seconds=80),
                }
            ]

    _patch_quote_clients(monkeypatch, lambda: _FakeCH())

    q, reason = strat._select_0dte_quote(now, side="CALL", spot=600.0, spread_max_pct=0.2)
    assert q is None
    assert reason == "STALE_QUOTE"


@pytest.mark.asyncio
async def test_get_mark_uses_recent_quote_within_age(monkeypatch):
    strat, _ctx = _make_strategy({"mark_quote_lookback_min": 2, "mark_quote_max_age_sec": 90})
    await strat.initialize()

    now = datetime(2026, 3, 5, 10, 10, 0, tzinfo=ET)
    opt_symbol = format_internal_opt_symbol(
        underlying="QQQ",
        expiry_ymd="20260305",
        strike=600.0,
        right="C",
    )

    class _FakeCH:
        def query(self, _sql, parameters=None):
            return [
                {
                    "ts": now - timedelta(minutes=1),
                    "mid": 1.25,
                    "created_at": now - timedelta(seconds=50),
                },
                {
                    "ts": now - timedelta(minutes=2),
                    "mid": 1.20,
                    "created_at": now - timedelta(seconds=110),
                },
            ]

    _patch_quote_clients(monkeypatch, lambda: _FakeCH())

    mark = strat._get_mark_for_contract(now, opt_symbol)
    assert mark == pytest.approx(1.25)


@pytest.mark.asyncio
async def test_submit_entry_logs_reject_reason_when_quote_missing(monkeypatch):
    strat, ctx = _make_strategy({"contracts": 3})
    await strat.initialize()

    now = datetime(2026, 3, 5, 10, 0, 0, tzinfo=ET)
    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (None, "NO_QUOTES"),
    )

    await strat._submit_entry(now, side="CALL", spot=600.0, entry_bar={"vwap": 599.8}, spread_max_pct=0.2)
    assert ctx.submit_intent.await_count == 0
    assert ctx.log.warning.call_count >= 1


@pytest.mark.asyncio
async def test_submit_entry_emits_local_audit_with_stable_ids(monkeypatch):
    strat, ctx = _make_strategy({"contracts": 3})
    writer = MagicMock()
    ctx.engine.audit_writer = writer
    await strat.initialize()

    now = datetime(2026, 3, 5, 10, 0, 0, tzinfo=ET)
    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260305:600:C",
                "strike": 600.0,
                "bid": 1.0,
                "ask": 1.1,
                "mid": 1.05,
                "last": 1.05,
                "quote_age_sec": 5.0,
            },
            None,
        ),
    )

    await strat._submit_entry(
        now,
        side="CALL",
        spot=600.0,
        entry_bar={"vwap": 599.8},
        spread_max_pct=0.2,
        audit_ids=("trigger-1", "decision-1", "trace-1"),
    )

    stages = [call.kwargs["stage"] for call in writer.emit.call_args_list]
    assert "contract_selected" in stages
    assert "quote_snapshot" in stages
    assert "entry_intent_requested" in stages
    assert "entry_intent_submitted" in stages
    requested = next(call.kwargs for call in writer.emit.call_args_list if call.kwargs["stage"] == "entry_intent_requested")
    submitted = next(call.kwargs for call in writer.emit.call_args_list if call.kwargs["stage"] == "entry_intent_submitted")
    assert requested["decision_id"] == "decision-1"
    assert requested["trigger_id"] == "trigger-1"


@pytest.mark.asyncio
async def test_submit_entry_rejects_tail_otm_put(monkeypatch):
    strat, ctx = _make_strategy({"contracts": 3})
    await strat.initialize()

    now = datetime(2026, 3, 25, 14, 57, 0, tzinfo=ET)
    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260325:585:P",
                "strike": 585.0,
                "bid": 0.38,
                "ask": 0.39,
                "mid": 0.385,
                "last": 0.39,
                "quote_age_sec": 5.0,
            },
            None,
        ),
    )

    await strat._submit_entry(now, side="PUT", spot=586.72, entry_bar={"vwap": 587.9}, spread_max_pct=0.2)

    assert ctx.submit_intent.await_count == 0
    assert ctx.log.warning.call_count >= 1
    assert any(call.args and call.args[0] == "qqq_0dte_entry_rejected_tail_otm_disallowed" for call in ctx.log.warning.call_args_list)


@pytest.mark.asyncio
async def test_submit_entry_allows_tail_atm_put(monkeypatch):
    strat, ctx = _make_strategy({"contracts": 3})
    await strat.initialize()

    now = datetime(2026, 3, 25, 14, 57, 0, tzinfo=ET)
    monkeypatch.setattr(
        strat,
        "_select_0dte_quote",
        lambda *args, **kwargs: (
            {
                "opt_symbol": "OPT:QQQ:20260325:587:P",
                "strike": 587.0,
                "bid": 0.52,
                "ask": 0.54,
                "mid": 0.53,
                "last": 0.53,
                "quote_age_sec": 5.0,
            },
            None,
        ),
    )

    await strat._submit_entry(now, side="PUT", spot=586.72, entry_bar={"vwap": 587.9}, spread_max_pct=0.2)

    assert ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_record_entry_block_emits_local_audit():
    strat, ctx = _make_strategy({})
    writer = MagicMock()
    ctx.engine.audit_writer = writer
    await strat.initialize()
    writer.emit.reset_mock()
    now = datetime(2026, 3, 5, 10, 1, 0, tzinfo=ET)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(strat, "_latest_quote_status", lambda *_args, **_kwargs: {"source": "memory", "age_sec": 11.0})

    strat._record_entry_block(now, "NO_BREAKOUT")
    monkeypatch.undo()

    writer.emit.assert_called_once()
    kwargs = writer.emit.call_args.kwargs
    assert kwargs["stage"] == "gate_blocked"
    assert kwargs["reason"] == "NO_BREAKOUT"
    payload = kwargs["payload"]
    assert payload["quote_source"] == "memory"
    assert payload["quote_age_sec"] == 11.0


@pytest.mark.asyncio
async def test_minute_heartbeat_audit_contains_quote_status(monkeypatch):
    strat, ctx = _make_strategy({})
    writer = MagicMock()
    ctx.engine.audit_writer = writer
    await strat.initialize()
    now = datetime(2026, 3, 5, 10, 2, 0, tzinfo=ET)
    monkeypatch.setattr(strat, "_latest_quote_status", lambda *_args, **_kwargs: {"source": "clickhouse", "age_sec": 33.0})

    strat._log_minute_heartbeat(now, {"close": 600.0, "vwap": 599.0})

    stages = [call.kwargs.get("stage") for call in writer.emit.call_args_list]
    assert "minute_heartbeat" in stages
    row = next(call.kwargs for call in writer.emit.call_args_list if call.kwargs.get("stage") == "minute_heartbeat")
    assert row["payload"]["quote_source"] == "clickhouse"
    assert row["payload"]["quote_age_sec"] == 33.0


@pytest.mark.asyncio
async def test_quote_hb_pause_then_resume(monkeypatch):
    extras = {
        "quote_hb_gate_enabled": True,
        "quote_hb_check_start": "09:45:00",
        "quote_hb_check_end": "14:50:00",
        "quote_hb_warmup_min": 0,
        "quote_hb_max_age_sec": 120,
        "quote_hb_fail_streak_to_pause": 2,
        "quote_hb_resume_fresh_streak": 1,
    }
    strat, _ctx = _make_strategy(extras)
    await strat.initialize()
    now = datetime(2026, 3, 5, 10, 0, 0, tzinfo=ET)

    class _FakeCH:
        def __init__(self):
            self.created_at = now - timedelta(seconds=500)

        def query(self, _sql, parameters=None):
            return [{"last_created_at": self.created_at, "last_ts": self.created_at}]

    fake = _FakeCH()
    _patch_quote_clients(monkeypatch, lambda: fake)

    # First stale check does not pause yet.
    assert strat._quote_heartbeat_allows_entry(now) is True
    # Second stale check reaches threshold -> pause.
    assert strat._quote_heartbeat_allows_entry(now + timedelta(seconds=30)) is False
    assert strat._quote_hb_pause_new_entries is True

    # Fresh quote should resume immediately (resume_streak=1).
    fake.created_at = now + timedelta(minutes=1) - timedelta(seconds=10)
    assert strat._quote_heartbeat_allows_entry(now + timedelta(minutes=1)) is True
    assert strat._quote_hb_pause_new_entries is False


@pytest.mark.asyncio
async def test_manage_position_forces_eod_flatten_before_mark_lookup(monkeypatch):
    strat, ctx = _make_strategy({"force_flatten_time": "15:58:00"})
    await strat.initialize()

    now = datetime(2026, 3, 10, 15, 58, 5, tzinfo=ET)
    bar = {"close": 600.0, "vwap": 599.5, "atr_d": 5.0}

    strat._pos = _PositionState(
        opt_symbol="OPT:QQQ:20260310:610:P",
        side="PUT",
        entry_time=now - timedelta(minutes=3),
        entry_underlying=602.0,
        tp1_done=False,
        filled=True,
        last_exit_reason=None,
    )

    ctx.position = AsyncMock(return_value=_Pos(qty=3.0, avg_px=2.07))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.portfolio = AsyncMock(return_value={"positions": []})

    submit_exit = AsyncMock()
    monkeypatch.setattr(strat, "_submit_exit", submit_exit)
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *args, **kwargs: None)

    handled = await strat._sync_and_manage_position(now, bar)

    assert handled is True
    submit_exit.assert_awaited_once()
    _, kwargs = submit_exit.await_args
    assert kwargs["reason"] == "EOD_FORCE_FLATTEN"
    assert kwargs["sell_qty"] == 3


@pytest.mark.asyncio
async def test_manage_position_uses_manual_flatten_reason_when_position_disappears():
    strat, ctx = _make_strategy({})
    await strat.initialize()

    now = datetime(2026, 3, 10, 11, 5, 0, tzinfo=ET)
    bar = {"close": 600.0, "vwap": 599.5, "atr_d": 5.0}

    strat._pos = _PositionState(
        opt_symbol="OPT:QQQ:20260310:610:P",
        side="PUT",
        entry_time=now - timedelta(minutes=5),
        entry_underlying=602.0,
        tp1_done=False,
        filled=True,
        last_exit_reason=None,
    )

    ctx.position = AsyncMock(return_value=_Pos(qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.portfolio = AsyncMock(return_value={"positions": []})

    handled = await strat._sync_and_manage_position(now, bar)

    assert handled is False
    assert strat._pos is None
    assert strat._first_trade_exit_reason == "MANUAL_FLATTEN"


@pytest.mark.asyncio
async def test_submit_exit_uses_degraded_mode_when_nbbo_is_fresh(monkeypatch):
    strat, ctx = _make_strategy({"exit_degraded_nbbo_max_age_sec": 5.0, "exit_tif": "IOC"})
    await strat.initialize()
    now = datetime(2026, 3, 11, 11, 31, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260311:610:P"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="PUT",
        entry_time=now - timedelta(minutes=5),
        entry_underlying=605.0,
        tp1_done=False,
        filled=True,
    )

    monkeypatch.setattr(
        strat,
        "_get_quote_for_contract",
        lambda *args, **kwargs: {"bid": 2.00, "ask": 2.10, "mid": 2.05, "age_sec": 2.0},
    )

    await strat._submit_exit(now, opt_symbol, sell_qty=3, reason="TP1")
    env = ctx.submit_intent.await_args.args[0]
    assert env.order_style.kind == "LMT"
    assert env.extras["tif"] == "IOC"
    assert env.order_style.limit_px is not None
    assert env.order_style.limit_px < 2.00  # marketable sell
    assert env.extras["exit_exec_mode"] == "EXIT_DEGRADED"


@pytest.mark.asyncio
async def test_submit_exit_uses_emergency_ladder_when_quote_is_stale(monkeypatch):
    strat, ctx = _make_strategy(
        {"exit_degraded_nbbo_max_age_sec": 5.0, "exit_emergency_ladder": [0.98, 0.95], "exit_tif": "IOC"}
    )
    await strat.initialize()
    now = datetime(2026, 3, 11, 13, 1, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260311:610:P"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="PUT",
        entry_time=now - timedelta(minutes=20),
        entry_underlying=608.0,
        tp1_done=True,
        filled=True,
    )

    monkeypatch.setattr(
        strat,
        "_get_quote_for_contract",
        lambda *args, **kwargs: {"bid": 2.00, "ask": 2.10, "mid": 2.05, "age_sec": 9.0},
    )

    await strat._submit_exit(now, opt_symbol, sell_qty=1, reason="RUNNER_PROTECT")
    env = ctx.submit_intent.await_args.args[0]
    assert env.order_style.kind == "LMT"
    assert env.extras["tif"] == "IOC"
    assert env.extras["exit_exec_mode"] == "EXIT_EMERGENCY"
    # First emergency step uses 0.98 multiplier -> rounded to tick.
    assert env.order_style.limit_px == pytest.approx(round(2.05 * 0.98, 2))


@pytest.mark.asyncio
async def test_submit_exit_blocks_when_working_exit_already_covers_position(monkeypatch):
    strat, ctx = _make_strategy({"exit_degraded_nbbo_max_age_sec": 5.0, "exit_tif": "IOC"})
    await strat.initialize()
    now = datetime(2026, 3, 24, 11, 37, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260324:585:P"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="PUT",
        entry_time=now - timedelta(minutes=8),
        entry_underlying=587.12,
        tp1_done=True,
        filled=True,
        trace_id="trace-1",
    )
    ctx.open_orders = AsyncMock(
        return_value=[
            _OpenOrder(
                symbol=opt_symbol,
                side="SELL",
                status="Submitted",
                remaining=1.0,
                order_id=25,
                order_ref="opt0dte_exit:qqq_0dte_opt_live_001:existing",
            )
        ]
    )
    monkeypatch.setattr(
        strat,
        "_get_quote_for_contract",
        lambda *args, **kwargs: {"bid": 0.95, "ask": 0.96, "mid": 0.955, "age_sec": 2.0},
    )

    submitted = await strat._submit_exit(
        now,
        opt_symbol,
        sell_qty=1,
        reason="RUNNER_PROTECT",
        current_qty=1.0,
    )

    assert submitted == 0
    ctx.submit_intent.assert_not_awaited()
    assert strat._pos.exit_attempts == 0
    assert any(call.args and call.args[0] == "qqq_0dte_exit_guard_blocked" for call in ctx.log.warning.call_args_list)


@pytest.mark.asyncio
async def test_submit_exit_partially_allows_only_uncovered_contracts(monkeypatch):
    strat, ctx = _make_strategy({"exit_degraded_nbbo_max_age_sec": 5.0, "exit_tif": "IOC"})
    await strat.initialize()
    now = datetime(2026, 3, 24, 11, 33, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260324:585:P"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="PUT",
        entry_time=now - timedelta(minutes=5),
        entry_underlying=587.12,
        tp1_done=False,
        filled=True,
        trace_id="trace-2",
    )
    ctx.open_orders = AsyncMock(
        return_value=[
            _OpenOrder(
                symbol=opt_symbol,
                side="SELL",
                status="Submitted",
                remaining=1.0,
                order_id=25,
                order_ref="opt0dte_exit:qqq_0dte_opt_live_001:existing",
            )
        ]
    )
    monkeypatch.setattr(
        strat,
        "_get_quote_for_contract",
        lambda *args, **kwargs: {"bid": 1.10, "ask": 1.11, "mid": 1.105, "age_sec": 2.0},
    )

    submitted = await strat._submit_exit(
        now,
        opt_symbol,
        sell_qty=2,
        reason="TP1",
        current_qty=2.0,
    )

    assert submitted == 1
    env = ctx.submit_intent.await_args.args[0]
    assert env.target_qty == pytest.approx(-1.0)
    assert strat._pos.exit_attempts == 1
    assert any(call.args and call.args[0] == "qqq_0dte_exit_guard_partial_allow" for call in ctx.log.warning.call_args_list)


@pytest.mark.asyncio
async def test_manage_position_does_not_duplicate_runner_exit_with_open_sell(monkeypatch):
    strat, ctx = _make_strategy({})
    await strat.initialize()
    now = datetime(2026, 3, 24, 11, 37, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260324:585:P"
    bar = {"close": 587.07, "vwap": 586.9, "atr_d": 5.0}

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="PUT",
        entry_time=now - timedelta(minutes=9),
        entry_underlying=587.12,
        tp1_done=True,
        filled=True,
        trace_id="trace-3",
    )

    ctx.position = AsyncMock(return_value=_Pos(qty=1.0, avg_px=0.90))
    ctx.open_orders = AsyncMock(
        return_value=[
            _OpenOrder(
                symbol=opt_symbol,
                side="SELL",
                status="Submitted",
                remaining=1.0,
                order_id=27,
                order_ref="opt0dte_exit:qqq_0dte_opt_live_001:runner",
            )
        ]
    )
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *args, **kwargs: 0.955)

    handled = await strat._sync_and_manage_position(now, bar)

    assert handled is True
    ctx.submit_intent.assert_not_awaited()
    assert any(call.args and call.args[0] == "qqq_0dte_exit_guard_blocked" for call in ctx.log.warning.call_args_list)


@pytest.mark.asyncio
async def test_hard_early_fail_triggers_for_call_without_first_two_bar_breakout(monkeypatch):
    strat, ctx = _make_strategy({"enable_early_cont_fail": True, "early_cont_fail_bars": 2})
    await strat.initialize()
    now = datetime(2026, 3, 24, 11, 30, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260324:600:C"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="CALL",
        entry_time=datetime(2026, 3, 24, 11, 28, 1, tzinfo=ET),
        entry_underlying=100.0,
        entry_close=100.0,
        entry_vwap=99.8,
        entry_atr_1m=0.5,
        tp1_done=False,
        filled=True,
        trace_id="early-hard-call",
    )
    strat._bars.extend(
        [
            {"ts": datetime(2026, 3, 24, 11, 29, 0, tzinfo=ET), "high": 99.95, "low": 99.70, "close": 99.90, "atr_1m": 0.5, "vwap": 99.8},
            {"ts": datetime(2026, 3, 24, 11, 30, 0, tzinfo=ET), "high": 100.00, "low": 99.75, "close": 99.88, "atr_1m": 0.5, "vwap": 99.8},
        ]
    )
    ctx.position = AsyncMock(return_value=_Pos(qty=3.0, avg_px=1.0))
    ctx.open_orders = AsyncMock(return_value=[])
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *args, **kwargs: 1.0)
    strat._submit_exit = AsyncMock(return_value=3)

    handled = await strat._sync_and_manage_position(now, strat._bars[-1])

    assert handled is True
    strat._submit_exit.assert_awaited_once()
    assert strat._submit_exit.await_args.kwargs["reason"] == "EARLY_CONT_FAIL"
    assert strat._pos.early_fail_triggered is True
    assert any(call.args and call.args[0] == "early_cont_fail_triggered" for call in ctx.log.warning.call_args_list)


@pytest.mark.asyncio
async def test_exp25_soft_exemption_selected_for_counter_vwap_reversal(monkeypatch):
    strat, ctx = _make_strategy(
        {
            "enable_early_cont_fail": True,
            "early_cont_fail_bars": 2,
            "early_cont_exempt_first1_move_atr1": 0.08,
            "early_cont_exempt_first1_max_giveback": 0.50,
        }
    )
    await strat.initialize()
    now = datetime(2026, 3, 24, 11, 30, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260324:605:C"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="CALL",
        entry_time=datetime(2026, 3, 24, 11, 28, 1, tzinfo=ET),
        entry_underlying=100.10,
        entry_close=100.00,
        entry_vwap=100.50,
        entry_atr_1m=0.5,
        tp1_done=False,
        filled=True,
        trace_id="early-soft-call",
        entry_counter_vwap=True,
    )
    strat._bars.extend(
        [
            {"ts": datetime(2026, 3, 24, 11, 29, 0, tzinfo=ET), "high": 100.18, "low": 100.00, "close": 100.14, "atr_1m": 0.5, "vwap": 100.4},
            {"ts": datetime(2026, 3, 24, 11, 30, 0, tzinfo=ET), "high": 100.20, "low": 100.02, "close": 100.15, "atr_1m": 0.5, "vwap": 100.3},
        ]
    )
    ctx.position = AsyncMock(return_value=_Pos(qty=3.0, avg_px=1.0))
    ctx.open_orders = AsyncMock(return_value=[])
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *args, **kwargs: 1.0)
    strat._submit_exit = AsyncMock(return_value=0)

    handled = await strat._sync_and_manage_position(now, strat._bars[-1])

    assert handled is True
    assert strat._pos.early_fail_mode == "soft_exempt"
    assert strat._pos.early_fail_decided is True
    strat._submit_exit.assert_not_awaited()
    assert any(call.args and call.args[0] == "entry_early_fail_mode" for call in ctx.log.info.call_args_list)


@pytest.mark.asyncio
async def test_tp1_takes_priority_over_early_cont_fail(monkeypatch):
    strat, ctx = _make_strategy({"enable_early_cont_fail": True, "early_cont_fail_bars": 2, "tp1_mult": 1.15})
    await strat.initialize()
    now = datetime(2026, 3, 24, 11, 30, 0, tzinfo=ET)
    opt_symbol = "OPT:QQQ:20260324:600:C"

    strat._pos = _PositionState(
        opt_symbol=opt_symbol,
        side="CALL",
        entry_time=datetime(2026, 3, 24, 11, 28, 1, tzinfo=ET),
        entry_underlying=100.0,
        entry_close=100.0,
        entry_vwap=99.8,
        entry_atr_1m=0.5,
        tp1_done=False,
        filled=True,
        trace_id="tp1-priority",
    )
    strat._bars.extend(
        [
            {"ts": datetime(2026, 3, 24, 11, 29, 0, tzinfo=ET), "high": 99.90, "low": 99.70, "close": 99.80, "atr_1m": 0.5, "vwap": 99.8},
            {"ts": datetime(2026, 3, 24, 11, 30, 0, tzinfo=ET), "high": 99.95, "low": 99.75, "close": 99.85, "atr_1m": 0.5, "vwap": 99.8},
        ]
    )
    ctx.position = AsyncMock(return_value=_Pos(qty=3.0, avg_px=1.0))
    ctx.open_orders = AsyncMock(return_value=[])
    monkeypatch.setattr(strat, "_get_mark_for_contract", lambda *args, **kwargs: 1.16)
    strat._submit_exit = AsyncMock(return_value=2)

    handled = await strat._sync_and_manage_position(now, strat._bars[-1])

    assert handled is True
    strat._submit_exit.assert_awaited_once()
    assert strat._submit_exit.await_args.kwargs["reason"] == "TP1"
    assert strat._pos.tp1_done is True
    assert strat._pos.early_fail_triggered is False


@pytest.mark.asyncio
async def test_quote_health_gate_restores_first_verdict_from_log(tmp_path, monkeypatch):
    strat, _ctx = _make_strategy({"quote_health_min_coverage": 0.70})
    await strat.initialize()

    log_path = tmp_path / "trader.log"
    events = [
        {
            "timestamp": "2026-03-12T10:00:00.000000",
            "ts": "2026-03-12 10:00:00",
            "coverage_pct": 93.33,
            "block_day": False,
            "strategy_id": "qqq_0dte_opt_live_001",
            "underlying": "QQQ",
            "event": "qqq_0dte_quote_data_health_gate",
        },
        {
            "timestamp": "2026-03-12T12:40:00.000000",
            "ts": "2026-03-12 12:40:00",
            "coverage_pct": 0.0,
            "block_day": True,
            "strategy_id": "qqq_0dte_opt_live_001",
            "underlying": "QQQ",
            "event": "qqq_0dte_quote_data_health_gate",
        },
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(strat, "_resolve_trader_log_path", lambda: Path(log_path))

    class _ShouldNotQuery:
        def query(self, *_args, **_kwargs):
            raise AssertionError("should restore from log before querying")

    _patch_quote_clients(monkeypatch, lambda: _ShouldNotQuery())

    strat._update_quote_data_health_gate(datetime(2026, 3, 12, 12, 45, 0, tzinfo=ET))
    assert strat._quote_health_checked is True
    assert strat._quote_health_block_day is False
    assert strat._quote_health_cov_pct == pytest.approx(0.9333, rel=1e-3)


@pytest.mark.asyncio
async def test_quote_health_gate_threshold_mismatch_falls_back_to_query(tmp_path, monkeypatch):
    strat, _ctx = _make_strategy({"quote_health_min_coverage": 0.70})
    await strat.initialize()
    log_path = tmp_path / "trader.log"
    event = {
        "timestamp": "2026-03-12T10:00:00.000000",
        "ts": "2026-03-12 10:00:00",
        "coverage_pct": 100.0,
        "threshold_pct": 0.0,
        "block_day": False,
        "strategy_id": "qqq_0dte_opt_live_001",
        "underlying": "QQQ",
        "event": "qqq_0dte_quote_data_health_gate",
    }
    log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setattr(strat, "_resolve_trader_log_path", lambda: Path(log_path))

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return [{"covered": 0}]

    _patch_quote_clients(monkeypatch, lambda: _FakeCH())
    strat._update_quote_data_health_gate(datetime(2026, 3, 12, 12, 45, 0, tzinfo=ET))
    assert strat._quote_health_checked is True
    assert strat._quote_health_block_day is True


@pytest.mark.asyncio
async def test_quote_health_gate_uncertain_late_log_falls_back_to_query(tmp_path, monkeypatch):
    strat, _ctx = _make_strategy({"quote_health_min_coverage": 0.70})
    await strat.initialize()
    log_path = tmp_path / "trader.log"
    event = {
        "timestamp": "2026-03-12T12:40:00.000000",
        "ts": "2026-03-12 12:40:00",
        "coverage_pct": 100.0,
        "threshold_pct": 70.0,
        "block_day": False,
        "strategy_id": "qqq_0dte_opt_live_001",
        "underlying": "QQQ",
        "event": "qqq_0dte_quote_data_health_gate",
    }
    log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setattr(strat, "_resolve_trader_log_path", lambda: Path(log_path))

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return [{"covered_minutes": 14, "nbbo_minutes": 14}]

    _patch_quote_clients(monkeypatch, lambda: _FakeCH())
    strat._update_quote_data_health_gate(datetime(2026, 3, 12, 12, 45, 0, tzinfo=ET))
    assert strat._quote_health_checked is True
    assert strat._quote_health_block_day is False
    assert strat._quote_health_cov_pct == pytest.approx(14 / 15, rel=1e-6)


@pytest.mark.asyncio
async def test_quote_health_gate_recovers_when_recent_quotes_are_healthy(monkeypatch):
    strat, _ctx = _make_strategy(
        {
            "quote_health_recovery_enabled": True,
            "quote_health_recovery_window_min": 10,
            "quote_health_recovery_min_coverage": 0.90,
            "quote_health_recovery_min_nbbo_coverage": 0.90,
            "quote_health_recovery_max_last_age_sec": 120.0,
        }
    )
    await strat.initialize()
    strat._quote_health_checked = True
    strat._quote_health_block_day = True
    strat._quote_health_cov_pct = 0.0

    monkeypatch.setattr(
        strat,
        "_query_quote_minute_coverage",
        lambda *_args, **_kwargs: {"covered_minutes": 10, "nbbo_minutes": 10},
    )
    monkeypatch.setattr(strat, "_latest_quote_age_sec", lambda *_args, **_kwargs: 15.0)

    now = datetime(2026, 3, 12, 13, 30, 0, tzinfo=ET)
    strat._maybe_release_quote_data_health_gate(now)

    assert strat._quote_health_block_day is False
    assert strat.state["quote_health_gate"]["restored_from"] == "recovery_gate"


@pytest.mark.asyncio
async def test_quote_health_gate_restore_prefers_recovery_event(tmp_path, monkeypatch):
    strat, _ctx = _make_strategy({"quote_health_min_coverage": 0.70})
    await strat.initialize()

    log_path = tmp_path / "trader.log"
    events = [
        {
            "timestamp": "2026-03-12T10:00:00.000000",
            "ts": "2026-03-12 10:00:00",
            "coverage_pct": 0.0,
            "threshold_pct": 70.0,
            "block_day": True,
            "strategy_id": "qqq_0dte_opt_live_001",
            "underlying": "QQQ",
            "event": "qqq_0dte_quote_data_health_gate",
        },
        {
            "timestamp": "2026-03-12T13:00:00.000000",
            "ts": "2026-03-12 13:00:00",
            "coverage_pct": 100.0,
            "threshold_pct": 70.0,
            "strategy_id": "qqq_0dte_opt_live_001",
            "underlying": "QQQ",
            "event": "qqq_0dte_quote_data_health_gate_recovered",
        },
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(strat, "_resolve_trader_log_path", lambda: Path(log_path))

    class _ShouldNotQuery:
        def query(self, *_args, **_kwargs):
            raise AssertionError("should restore from recovery log before querying")

    _patch_quote_clients(monkeypatch, lambda: _ShouldNotQuery())

    strat._update_quote_data_health_gate(datetime(2026, 3, 12, 13, 30, 0, tzinfo=ET))
    assert strat._quote_health_checked is True
    assert strat._quote_health_block_day is False


@pytest.mark.asyncio
async def test_quote_health_gate_recovery_uses_completed_minutes_only(monkeypatch):
    strat, _ctx = _make_strategy(
        {
            "quote_health_recovery_enabled": True,
            "quote_health_recovery_window_min": 10,
            "quote_health_recovery_min_coverage": 0.90,
            "quote_health_recovery_min_nbbo_coverage": 0.90,
            "quote_health_recovery_max_last_age_sec": 120.0,
        }
    )
    await strat.initialize()
    strat._quote_health_checked = True
    strat._quote_health_block_day = True

    captured = {}

    def _fake_query(start, end):
        captured["start"] = start
        captured["end"] = end
        return {"covered_minutes": 10, "nbbo_minutes": 10}

    monkeypatch.setattr(strat, "_query_quote_minute_coverage", _fake_query)
    monkeypatch.setattr(strat, "_latest_quote_age_sec", lambda *_args, **_kwargs: 15.0)

    now = datetime(2026, 3, 12, 13, 34, 6, tzinfo=ET)
    strat._maybe_release_quote_data_health_gate(now)

    assert captured["end"] == datetime(2026, 3, 12, 13, 34, 0, tzinfo=ET)
    assert captured["start"] == datetime(2026, 3, 12, 13, 24, 0, tzinfo=ET)
    assert strat._quote_health_block_day is False


@pytest.mark.asyncio
async def test_restore_adopted_position_state_prefers_recent_position_diag(tmp_path, monkeypatch):
    strat, _ctx = _make_strategy({"contracts": 3})
    await strat.initialize()

    log_path = tmp_path / "trader.log"
    events = [
        {
            "timestamp": "2026-03-13T14:58:01.391804",
            "strategy_id": "qqq_0dte_opt_live_001",
            "trace_id": "0619f1f5-ab1",
            "symbol": "OPT:QQQ:20260313:595:C",
            "held_min": 28.0,
            "tp1_done": True,
            "event": "qqq_0dte_position_diag",
        }
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(strat, "_resolve_trader_log_path", lambda: Path(log_path))

    now = datetime(2026, 3, 13, 14, 59, 10, tzinfo=ET)
    restored = strat._restore_adopted_position_state(now, "OPT:QQQ:20260313:595:C", 1.0)

    assert restored["source"] == "position_diag_log"
    assert restored["trace_id"] == "0619f1f5-ab1"
    assert restored["tp1_done"] is True
    assert restored["entry_time"] == now - timedelta(minutes=28)


@pytest.mark.asyncio
async def test_restore_adopted_position_state_falls_back_to_qty_heuristic(monkeypatch):
    strat, _ctx = _make_strategy({"contracts": 3})
    await strat.initialize()
    monkeypatch.setattr(strat, "_resolve_trader_log_path", lambda: None)

    now = datetime(2026, 3, 13, 15, 0, 0, tzinfo=ET)
    restored = strat._restore_adopted_position_state(now, "OPT:QQQ:20260313:595:C", 1.0)

    assert restored["source"] == "heuristic_qty"
    assert restored["trace_id"] == "adopted"
    assert restored["tp1_done"] is True
    assert restored["entry_time"] == now
