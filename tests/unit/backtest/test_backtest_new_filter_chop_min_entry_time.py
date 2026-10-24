from __future__ import annotations

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


sys.path.insert(0, "scripts")
import backtest_new_filter_main as bt  # noqa: E402


ET = ZoneInfo("America/New_York")


def _build_synthetic_bars() -> list[bt.Tick]:
    start = datetime(2026, 1, 5, 9, 30, tzinfo=ET)
    closes = [
        100,
        101,
        99,
        102,
        98,
        103,
        97,
        104,
        96,
        105,
        95,
        106,
        94,
        107,
        106,
        105,
        104,
        103,
        102,
        101,
        100,
        99,
        98,
        97,
        96,
        95,
        94,
        93,
        92,
        91,
        90,
    ]
    bars: list[bt.Tick] = []
    for i, close_px in enumerate(closes):
        ts = start + timedelta(minutes=i)
        open_px = closes[i - 1] if i > 0 else close_px
        high_px = max(open_px, close_px) + 0.2
        low_px = min(open_px, close_px) - 0.2
        bars.append(
            bt.Tick(
                ts=ts,
                open=float(open_px),
                high=float(high_px),
                low=float(low_px),
                close=float(close_px),
                volume=1000.0,
            )
        )
    return bars


def _simulate_kwargs() -> dict:
    return {
        "day": "2026-01-05",
        "notional_usd": 10000,
        "target_atr_mult": 1.0,
        "tp2_trend_atr_mult": 0.0,
        "hard_stop_atr_mult": 1.0,
        "stop_min_abs": 0.0,
        "logic_retreat_minutes": 7,
        "logic_retreat_buffer_atr": 0.0,
        "atr_cap_mult": 1.8,
        "momentum_deadzone": 0.05,
        "fast_v3m_atr_min": 999.0,
        "fast_vol_min": 999.0,
        "drift_bar_min_atr": 0.1,
        "drift_cum_atr_min": 0.45,
        "fast_crosses_threshold": 3,
        "fast_cooldown_min": 15.0,
        "veto_rebound_live_atr": 0.25,
        "atr_seed_map": {},
        "atr_d": 1.0,
        "atr_rank": 0.1,
        "atr_rank_chop_max": 0.15,
        "early_breakout_move_atr": 9.0,
        "early_breakout_vol_min": 9.0,
        "chop_mode": "mr",
        "mr_tp_atr": 0.2,
        "mr_sl_atr": 5.0,
        "mr_max_hold_min": 30,
        "a_day_size_mult": 1.0,
        "a_day_atr_rank_min": 0.9,
        "drift_cum_atrd_min": 0.8,
        "early_lock_mode": "openclose",
        "delayed_confirm_move5m_min": 0.2,
        "delayed_confirm_end": bt.time(10, 0),
    }


def test_chop_branch_respects_min_entry_time(monkeypatch) -> None:
    bars = _build_synthetic_bars()

    def _fake_loader(day: str, symbol: str) -> list[bt.Tick]:
        return bars

    monkeypatch.setattr(bt, "load_1m_bars_from_clickhouse", _fake_loader)

    kwargs = _simulate_kwargs()
    outcome_first = bt.simulate_day(**kwargs, min_entry_time=None)
    assert outcome_first.trade is not None
    assert outcome_first.trade.entry_time == datetime(2026, 1, 5, 9, 45, tzinfo=ET)

    # After first trade exits, second pass must not re-use the same early MR entry.
    outcome_second = bt.simulate_day(**kwargs, min_entry_time=datetime(2026, 1, 5, 9, 50, tzinfo=ET))
    assert outcome_second.trade is None
    assert outcome_second.no_trade_reason == "mr_no_signal"
