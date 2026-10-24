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


def _make_strategy(strategy_id: str = "qqq_intraday_001") -> QQQIntradayStrategy:
    ctx = StrategyContext(
        engine=_DummyEngine(),
        strategy_id=strategy_id,
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    params.extras = {
        "entry_model": "v47_fast_drift",
        "enable_v47_chase_guard": True,
        "v47_chase_guard_start": "09:45:00",
        "v47_chase_guard_end": "11:50:00",
        "v47_long_pos_day_min": 0.80,
        "v47_short_pos_day_max": 0.20,
        "v47_long_dev_vwap_min": 0.0020,
        "v47_short_dev_vwap_min": 0.0020,
    }
    return QQQIntradayStrategy(ctx, params)


def _seed_session_bars(strategy: QQQIntradayStrategy, now: datetime, low: float, high: float) -> None:
    strategy._minute_bars.clear()
    for i in range(30):
        ts_start = now.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=i)
        ts_end = ts_start + timedelta(minutes=1)
        px = low + (high - low) * (i / 29.0)
        strategy._minute_bars.append(
            _OhlcvBar(ts_start=ts_start, ts_end=ts_end, open=px, high=px, low=px, close=px, volume=100.0)
        )


def test_v47_chase_guard_blocks_long_top_chase():
    s = _make_strategy()
    now = datetime(2026, 3, 6, 10, 10, 0, tzinfo=ET)
    _seed_session_bars(s, now, low=600.0, high=610.0)
    r = s._v47_chase_guard_reason(
        now=now,
        direction="LONG",
        qqq_now=609.8,
        vwap_now=608.0,
        extras=s.params.extras,
    )
    assert r is not None
    assert r["reason"] == "v47_long_chase_guard"


def test_v47_chase_guard_blocks_short_bottom_chase():
    s = _make_strategy()
    now = datetime(2026, 3, 6, 10, 20, 0, tzinfo=ET)
    _seed_session_bars(s, now, low=600.0, high=610.0)
    r = s._v47_chase_guard_reason(
        now=now,
        direction="SHORT",
        qqq_now=600.2,
        vwap_now=602.0,
        extras=s.params.extras,
    )
    assert r is not None
    assert r["reason"] == "v47_short_chase_guard"


def test_v47_chase_guard_allows_non_chase_entry():
    s = _make_strategy()
    now = datetime(2026, 3, 6, 10, 25, 0, tzinfo=ET)
    _seed_session_bars(s, now, low=600.0, high=610.0)
    r = s._v47_chase_guard_reason(
        now=now,
        direction="LONG",
        qqq_now=605.0,
        vwap_now=604.8,
        extras=s.params.extras,
    )
    assert r is None


def test_anti_chase_blocks_long_entry_near_15m_top():
    s = _make_strategy()
    now = datetime(2026, 3, 6, 10, 10, 0, tzinfo=ET)
    _seed_session_bars(s, now, low=600.0, high=610.0)
    r = s._anti_chase_reason(
        now=now,
        direction="LONG",
        mode="DRIFT",
        qqq_now=610.2,
        vwap_now=608.0,
        distance_from_vwap_atr=0.20,
        extras=s.params.extras,
    )
    assert r is not None
    assert r["reason"] == "anti_chase_blocked"
    assert r["rank15"] >= 0.93


def test_anti_chase_allows_entry_when_not_extended():
    s = _make_strategy()
    now = datetime(2026, 3, 6, 10, 10, 0, tzinfo=ET)
    _seed_session_bars(s, now, low=600.0, high=610.0)
    r = s._anti_chase_reason(
        now=now,
        direction="LONG",
        mode="DRIFT",
        qqq_now=608.6,
        vwap_now=608.0,
        distance_from_vwap_atr=0.10,
        extras=s.params.extras,
    )
    assert r is None


def test_r1_blocks_only_fourth_short_with_small_green_day():
    s = _make_strategy()
    s._success_count = 3
    s._day_realized_pnl = 50.0
    r = s._r1_fourth_short_small_green_block_reason(direction="SHORT", extras=s.params.extras)
    assert r is not None
    assert r["reason"] == "r1_fourth_short_small_green_blocked"
    assert r["projected_trade_seq"] == 4


def test_r1_does_not_block_fourth_long_or_day_pnl_at_100():
    s = _make_strategy()
    s._success_count = 3
    s._day_realized_pnl = 50.0
    assert s._r1_fourth_short_small_green_block_reason(direction="LONG", extras=s.params.extras) is None
    s._day_realized_pnl = 100.0
    assert s._r1_fourth_short_small_green_block_reason(direction="SHORT", extras=s.params.extras) is None


def test_recommended_filters_default_disabled_for_other_strategy_ids():
    s = _make_strategy(strategy_id="qqq_intraday_test")
    assert s._anti_chase_enabled(s.params.extras) is False
    assert s._r1_fourth_short_small_green_enabled(s.params.extras) is False
