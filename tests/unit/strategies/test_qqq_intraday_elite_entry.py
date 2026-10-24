import asyncio
import json
from datetime import datetime, timedelta

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import ET, QQQIntradayStrategy, _OhlcvBar


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _DummyEngine:
    pass


async def _noop_submit_target(symbol: str, qty: int) -> bool:
    return True


async def _noop_submit_stop(reason: str) -> None:
    return


def _make_strategy(tmp_path) -> QQQIntradayStrategy:
    ctx = StrategyContext(
        engine=_DummyEngine(),
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    extras = params.extras
    extras["entry_model"] = "v44_elite"
    extras["freshness_max_sec"] = 5.0
    extras["freshness_max_sec_qqew"] = 5.0
    extras["entry_max_data_age_sec"] = 5.0
    extras["atr_period_1m"] = 3
    extras["notional_usd"] = 10000
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._diag_path = tmp_path / "qqq_elite_diag.jsonl"
    strategy._submit_target = _noop_submit_target  # type: ignore[method-assign]
    strategy._submit_protective_stop_intent = _noop_submit_stop  # type: ignore[method-assign]
    strategy._calc_qty = lambda price: 1  # type: ignore[method-assign]
    strategy._exec_minute_close_for_gate = lambda symbol, minute_ts, now_dt: 49.5  # type: ignore[method-assign]
    strategy._effective_atr = lambda symbol, period, now=None: 1.0  # type: ignore[method-assign]
    strategy._stop_audit_started = True
    return strategy


def _prime_common_state(strategy: QQQIntradayStrategy, now: datetime) -> None:
    now_ms = int(now.timestamp() * 1000)
    strategy._day = now.strftime("%Y-%m-%d")
    strategy._is_traded = False
    strategy._side = None
    strategy._exec_symbol = None
    strategy._last_entry_eval_second = None
    strategy._success_count = 0
    strategy._pending_decision_id = None
    strategy._amp_locked = True

    strategy._last_seen_ts_ms["QQQ"] = now_ms
    strategy._last_seen_ts_ms["QQEW"] = now_ms
    strategy._last_seen_ts_ms["SQQQ"] = now_ms
    strategy._last_seen_ts_ms["TQQQ"] = now_ms

    strategy._rolling_vol_queue.clear()
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.clear()
    strategy._today_vol_series.extend([100.0] * 20)

    strategy._atr_pre["TQQQ"] = 1.0
    strategy._atr_pre["SQQQ"] = 1.0

    strategy._vwap_pv = 100.0 * 1000.0
    strategy._vwap_v = 1000.0
    strategy._qqew_vwap_pv = 50.0 * 1000.0
    strategy._qqew_vwap_v = 1000.0
    strategy._qqew_last_price = 49.7

    strategy._min_bar = _OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + timedelta(minutes=1),
        open=99.4,
        high=99.6,
        low=99.2,
        close=99.5,
        volume=200.0,
    )

    strategy._cur_5m["SQQQ"] = _OhlcvBar(
        ts_start=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0),
        ts_end=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) + timedelta(minutes=5),
        open=75.0,
        high=75.2,
        low=74.8,
        close=75.0,
        volume=1000.0,
    )
    strategy._cur_5m["TQQQ"] = _OhlcvBar(
        ts_start=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0),
        ts_end=now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0) + timedelta(minutes=5),
        open=48.0,
        high=48.2,
        low=47.8,
        close=48.0,
        volume=1000.0,
    )

    strategy._qqq_buffer.clear()
    strategy._qqew_buffer.clear()
    # Need at least [now-183s, now] so elite 3s decay window can look back
    # and still resolve each (ts_ref - 180s) anchor.
    for i in range(184):
        ts = now_ms - (183 - i) * 1000
        qqq_px = 100.0 - (i / 183.0) * 0.5
        qqew_px = 50.0 - (i / 183.0) * 0.3
        strategy._qqq_buffer.append((ts, qqq_px))
        strategy._qqew_buffer.append((ts, qqew_px))


def _set_last_seconds(strategy: QQQIntradayStrategy, now: datetime, sec_to_price: dict[int, float]) -> None:
    ts_to_price = {
        int((now - timedelta(seconds=sec)).timestamp() * 1000): float(px)
        for sec, px in sec_to_price.items()
    }
    out = []
    for ts_ms, px in strategy._qqq_buffer:
        out.append((ts_ms, ts_to_price.get(ts_ms, px)))
    strategy._qqq_buffer.clear()
    strategy._qqq_buffer.extend(out)


def _last_gate_reason(diag_path) -> str:
    lines = [x for x in diag_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for line in reversed(lines):
        obj = json.loads(line)
        if obj.get("event") == "entry_gate":
            return str(obj.get("reason") or "")
    return ""


def _last_gate_record(diag_path) -> dict:
    lines = [x for x in diag_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for line in reversed(lines):
        obj = json.loads(line)
        if obj.get("event") == "entry_gate":
            return obj
    return {}


def test_elite_entry_allowed_short(tmp_path):
    strategy = _make_strategy(tmp_path)
    now = datetime(2026, 2, 17, 10, 7, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    # Current v3m=-0.5; recent max abs around 0.52 -> decay pass.
    _set_last_seconds(strategy, now, {0: 99.5, 1: 99.48, 2: 99.52, 3: 99.50})
    strategy._min_bar.close = 99.5
    strategy._min_bar.volume = 220.0
    strategy._qqew_last_price = 49.6

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is True
    assert strategy._exec_symbol == "SQQQ"


def test_elite_blocks_overextended(tmp_path):
    strategy = _make_strategy(tmp_path)
    now = datetime(2026, 2, 17, 10, 8, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    # Overextended: |price-vwap|=2.0 > 1.3*atr_eff(=1.3)
    strategy._min_bar.close = 98.0
    _set_last_seconds(strategy, now, {0: 98.0, 1: 98.1, 2: 98.2, 3: 98.3})
    strategy._qqew_last_price = 49.0

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert _last_gate_reason(strategy._diag_path) == "elite_overextended"


def test_elite_blocks_momentum_decay(tmp_path):
    strategy = _make_strategy(tmp_path)
    now = datetime(2026, 2, 17, 10, 9, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 0.6  # type: ignore[method-assign]
    # v3m now abs=0.30, but previous max abs=0.50 => 0.30 < 0.95*0.50 blocked.
    _set_last_seconds(strategy, now, {0: 99.70, 1: 99.50, 2: 99.50, 3: 99.50})
    strategy._min_bar.close = 99.70
    strategy._qqew_last_price = 49.7

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert _last_gate_reason(strategy._diag_path) == "elite_momentum_decay"


def test_elite_blocks_high_vol_sign_not_confirmed(tmp_path):
    strategy = _make_strategy(tmp_path)
    now = datetime(2026, 2, 17, 10, 10, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 2.0  # type: ignore[method-assign]
    # High-vol path enabled; now sign SHORT(<0), previous sample sign flips (>0).
    _set_last_seconds(strategy, now, {0: 99.00, 1: 100.40, 2: 99.80, 3: 99.70})
    strategy._min_bar.close = 99.00
    strategy._qqew_last_price = 49.0

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert _last_gate_reason(strategy._diag_path) == "elite_high_vol_sign_not_confirmed"


def test_elite_pullback_reentry_waits_after_overextended(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["elite_max_ext_mult"] = 1.6
    strategy.params.extras["elite_enable_pullback_reentry"] = True
    strategy.params.extras["elite_reentry_window_sec"] = 90
    strategy.params.extras["elite_reentry_ext_mult"] = 1.1
    now = datetime(2026, 2, 17, 10, 11, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    strategy._qqew_last_price = 49.0

    # First sample: overextended.
    strategy._min_bar.close = 98.0
    _set_last_seconds(strategy, now, {0: 98.0, 1: 98.1, 2: 98.2, 3: 98.3})
    asyncio.run(strategy._maybe_enter(now))
    assert _last_gate_reason(strategy._diag_path) == "elite_overextended"

    # Second sample: back inside 1.6*ATR but still above reentry band 1.1*ATR.
    now2 = now + timedelta(seconds=1)
    now2_ms = int(now2.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now2_ms
    strategy._last_seen_ts_ms["QQEW"] = now2_ms
    strategy._last_seen_ts_ms["SQQQ"] = now2_ms
    strategy._min_bar.close = 98.8
    _set_last_seconds(strategy, now2, {0: 98.8, 1: 98.9, 2: 99.0, 3: 99.1})
    asyncio.run(strategy._maybe_enter(now2))
    assert strategy._is_traded is False
    assert _last_gate_reason(strategy._diag_path) == "elite_reentry_wait_pullback"


def test_elite_decoupled_ignores_qqew_resonance(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["elite_decouple_qqew"] = True
    strategy.params.extras["elite_ignore_qqew_resonance"] = True
    strategy.params.extras["entry_max_data_age_sec_qqew"] = 15.0
    now = datetime(2026, 2, 17, 10, 12, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    strategy._min_bar.close = 99.5
    strategy._min_bar.volume = 220.0
    # QQEW intentionally on wrong side of VWAP for SHORT, should be ignored in decoupled mode.
    strategy._qqew_last_price = 50.4
    _set_last_seconds(strategy, now, {0: 99.5, 1: 99.48, 2: 99.52, 3: 99.50})

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is True
    assert strategy._exec_symbol == "SQQQ"


def test_v46_ab_opening_mode_breakout_entry(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["entry_model"] = "v46_ab"
    strategy.params.extras["elite_ab_dual_mode"] = True
    strategy.params.extras["ab_time_start"] = "09:32:00"
    strategy.params.extras["ab_time_end"] = "10:30:00"
    strategy.params.extras["entry_trade_allow_data_age_sec_qqq"] = 10.0
    strategy.params.extras["entry_max_data_age_sec_qqq"] = 15.0
    strategy.params.extras["entry_require_continuity_180s"] = False

    now = datetime(2026, 2, 17, 9, 35, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    strategy._min_bar.close = 99.5
    strategy._min_bar.volume = 220.0
    _set_last_seconds(strategy, now, {0: 99.5, 1: 99.48, 2: 99.52, 3: 99.50})
    minute_end = now.replace(second=0, microsecond=0)
    strategy._min_closes[minute_end - timedelta(minutes=3)] = 100.2
    strategy._min_closes[minute_end - timedelta(minutes=1)] = 100.0
    strategy._min_closes[minute_end] = 99.6

    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms
    strategy._last_seen_ts_ms["QQEW"] = now_ms
    strategy._last_seen_ts_ms["SQQQ"] = now_ms
    strategy._last_seen_ts_ms["TQQQ"] = now_ms
    strategy._min_bar.ts_start = now.replace(second=0, microsecond=0)
    strategy._min_bar.ts_end = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    strategy._min_bar.volume = 220.0
    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is True
    assert strategy._exec_symbol == "SQQQ"


def test_v46_ab_opening_mode_blocks_non_breakout(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["entry_model"] = "v46_ab"
    strategy.params.extras["elite_ab_dual_mode"] = True
    strategy.params.extras["ab_time_start"] = "09:32:00"
    strategy.params.extras["ab_time_end"] = "10:30:00"
    strategy.params.extras["entry_trade_allow_data_age_sec_qqq"] = 10.0
    strategy.params.extras["entry_max_data_age_sec_qqq"] = 15.0
    strategy.params.extras["entry_require_continuity_180s"] = False

    now = datetime(2026, 2, 17, 9, 35, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    strategy._min_bar.close = 99.95
    strategy._min_bar.volume = 220.0
    _set_last_seconds(strategy, now, {0: 99.95, 1: 99.95, 2: 99.95, 3: 99.95})
    minute_end = now.replace(second=0, microsecond=0)
    strategy._min_closes[minute_end - timedelta(minutes=3)] = 100.1
    strategy._min_closes[minute_end - timedelta(minutes=1)] = 100.0
    strategy._min_closes[minute_end] = 99.95

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert strategy._ab_a_state == "WAIT_MARK"
    assert _last_gate_reason(strategy._diag_path) in {"ab_opening_velocity_too_low", "ab_opening_sign_mismatch"}


def test_entry_gate_diag_backfills_missing_metrics(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["entry_model"] = "v44_elite"
    now = datetime(2026, 2, 18, 10, 1, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    strategy._min_bar.close = 100.2
    strategy._min_bar.volume = 220.0
    strategy._qqew_last_price = 50.2
    strategy._vwap_pv = 100.0 * 1000.0
    strategy._vwap_v = 1000.0
    strategy._qqew_vwap_pv = 50.0 * 1000.0
    strategy._qqew_vwap_v = 1000.0
    _set_last_seconds(strategy, now, {0: 100.2, 1: 100.25, 2: 100.27, 3: 100.3})

    asyncio.run(strategy._maybe_enter(now))
    rec = _last_gate_record(strategy._diag_path)

    assert rec.get("event") == "entry_gate"
    assert rec.get("decision") == "BLOCKED"
    assert float(rec.get("qqq_close_now", 0.0)) > 0.0
    assert float(rec.get("vwap_now", 0.0)) > 0.0
    assert float(rec.get("qqew_close_now", 0.0)) > 0.0
    assert float(rec.get("qqew_vwap_now", 0.0)) > 0.0
    assert float(rec.get("qqq_3m_ago", 0.0)) > 0.0
    assert float(rec.get("data_age_qqq_sec", -1.0)) >= 0.0


def test_v46_ab_a_pullback_entry_after_mark(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["entry_model"] = "v46_ab"
    strategy.params.extras["elite_ab_dual_mode"] = True
    strategy.params.extras["ab_time_start"] = "09:32:00"
    strategy.params.extras["ab_time_end"] = "10:30:00"
    strategy.params.extras["entry_trade_allow_data_age_sec_qqq"] = 10.0
    strategy.params.extras["entry_max_data_age_sec_qqq"] = 15.0
    strategy.params.extras["entry_require_continuity_180s"] = False

    now = datetime(2026, 2, 17, 9, 57, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]

    # Step 1: mark overextended.
    strategy._min_bar.close = 98.0
    strategy._min_bar.volume = 220.0
    _set_last_seconds(strategy, now, {0: 98.0, 1: 98.1, 2: 98.2, 3: 98.3})
    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert strategy._ab_a_state == "WAIT_PULLBACK"

    # Step 2: pullback into 1.5 ATR and enter.
    now2 = now + timedelta(seconds=1)
    now2_ms = int(now2.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now2_ms
    strategy._last_seen_ts_ms["QQEW"] = now2_ms
    strategy._last_seen_ts_ms["SQQQ"] = now2_ms
    strategy._last_seen_ts_ms["TQQQ"] = now2_ms
    strategy._min_bar.close = 98.8
    strategy._min_bar.volume = 220.0
    _set_last_seconds(strategy, now2, {0: 98.8, 1: 98.9, 2: 99.0, 3: 99.1})
    asyncio.run(strategy._maybe_enter(now2))
    assert strategy._is_traded is True
    assert strategy._exec_symbol == "SQQQ"


def test_v46_ab_a_entry_max_dev_live_atr_blocks_chase(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["entry_model"] = "v46_ab"
    strategy.params.extras["elite_ab_dual_mode"] = True
    strategy.params.extras["ab_time_start"] = "09:32:00"
    strategy.params.extras["ab_time_end"] = "10:30:00"
    strategy.params.extras["entry_trade_allow_data_age_sec_qqq"] = 10.0
    strategy.params.extras["entry_max_data_age_sec_qqq"] = 15.0
    strategy.params.extras["entry_require_continuity_180s"] = False
    strategy.params.extras["a_entry_max_dev_live_atr"] = 1.0

    now = datetime(2026, 2, 17, 9, 57, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]

    strategy._min_bar.close = 98.0
    strategy._min_bar.volume = 220.0
    _set_last_seconds(strategy, now, {0: 98.0, 1: 98.1, 2: 98.2, 3: 98.3})
    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert strategy._ab_a_state == "WAIT_PULLBACK"

    now2 = now + timedelta(seconds=1)
    now2_ms = int(now2.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now2_ms
    strategy._last_seen_ts_ms["QQEW"] = now2_ms
    strategy._last_seen_ts_ms["SQQQ"] = now2_ms
    strategy._last_seen_ts_ms["TQQQ"] = now2_ms
    strategy._min_bar.close = 98.8
    strategy._min_bar.volume = 220.0
    _set_last_seconds(strategy, now2, {0: 98.8, 1: 98.9, 2: 99.0, 3: 99.1})
    asyncio.run(strategy._maybe_enter(now2))
    assert strategy._is_traded is False


def test_v46_ab_offhours_skips_entry_gate_logging(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["entry_model"] = "v46_ab"
    strategy.params.extras["elite_ab_dual_mode"] = True
    strategy.params.extras["ab_time_start"] = "09:32:00"
    strategy.params.extras["ab_time_end"] = "15:30:00"

    now = datetime(2026, 2, 17, 1, 5, 0, tzinfo=ET)
    _prime_common_state(strategy, now)
    strategy._atr_1m_qqq = lambda period=14: 1.0  # type: ignore[method-assign]
    strategy._min_bar.close = 99.5
    strategy._min_bar.volume = 220.0

    asyncio.run(strategy._maybe_enter(now))
    assert strategy._is_traded is False
    assert strategy._diag_path.exists() is False


def test_logic_retreat_respects_min_hold_seconds(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["logic_retreat_min_hold_sec"] = 360
    now = datetime(2026, 2, 17, 10, 5, 0, tzinfo=ET)
    _prime_common_state(strategy, now)

    strategy._is_traded = True
    strategy._side = "LONG"
    strategy._exec_symbol = "TQQQ"
    strategy._entry_px = 50.0
    strategy._locked_velocity_sign = 1
    strategy._entry_time_et = now - timedelta(seconds=60)
    strategy._min_closes[now.replace(second=0, microsecond=0)] = 99.0

    calls: list[str] = []

    async def _fake_flatten(reason: str) -> None:
        calls.append(reason)

    strategy._flatten = _fake_flatten  # type: ignore[method-assign]
    asyncio.run(strategy._maybe_logic_retreat(now))
    assert calls == []


def test_logic_retreat_triggers_after_min_hold_seconds(tmp_path):
    strategy = _make_strategy(tmp_path)
    strategy.params.extras["logic_retreat_min_hold_sec"] = 360
    now = datetime(2026, 2, 17, 10, 5, 0, tzinfo=ET)
    _prime_common_state(strategy, now)

    strategy._is_traded = True
    strategy._side = "LONG"
    strategy._exec_symbol = "TQQQ"
    strategy._entry_px = 50.0
    strategy._locked_velocity_sign = 1
    strategy._entry_time_et = now - timedelta(seconds=600)
    strategy._min_closes[now.replace(second=0, microsecond=0)] = 99.0

    calls: list[str] = []

    async def _fake_flatten(reason: str) -> None:
        calls.append(reason)

    strategy._flatten = _fake_flatten  # type: ignore[method-assign]
    asyncio.run(strategy._maybe_logic_retreat(now))
    assert calls == ["logic_retreat"]
