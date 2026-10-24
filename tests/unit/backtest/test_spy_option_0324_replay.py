from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from services.backtest.spy_option_0324_replay import (
    ContractQuote,
    OpeningRangeBreakoutStrategy,
    PendingEntry,
    PositionState,
    ReplayConfig,
    SpyOptionReplayBacktester,
    build_features,
    classify_signal_regime,
    detect_setup,
    explain_contract_rejection,
    entry_fill_price,
    exit_fill_price,
    select_contract,
)
from services.option_replay.core import market_data
from services.option_replay.core.market_data import clean_intraday_bars, load_option_quotes_for_replay, resolve_indicator_profile
from services.option_replay.core.audit import build_run_result
from services.option_replay.strategies import get_strategy, list_strategies


def _quote(ts: datetime, *, expiry: str, right: str, strike: float, dte: int, bid: float, ask: float, delta: float, oi: int = 500) -> ContractQuote:
    return ContractQuote(
        ts=ts,
        expiry=expiry,
        right=right,
        strike=strike,
        dte=dte,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2.0,
        iv=0.22,
        delta=delta,
        open_interest=oi,
    )


def test_select_contract_prefers_1dte_when_not_aggressive():
    ts = datetime(2026, 3, 24, 10, 0)
    cfg = ReplayConfig()
    chain = [
        _quote(ts, expiry="2026-03-24", right="C", strike=565.0, dte=0, bid=3.0, ask=3.3, delta=0.50),
        _quote(ts, expiry="2026-03-25", right="C", strike=565.0, dte=1, bid=3.2, ask=3.5, delta=0.51),
    ]
    selected = select_contract(chain, right="C", spot=565.2, aggressive_same_day=False, cfg=cfg)
    assert selected is not None
    assert selected.dte == 1


def test_select_contract_allows_0dte_when_aggressive():
    ts = datetime(2026, 3, 24, 10, 0)
    cfg = ReplayConfig()
    chain = [
        _quote(ts, expiry="2026-03-24", right="P", strike=560.0, dte=0, bid=2.8, ask=3.0, delta=-0.49),
        _quote(ts, expiry="2026-03-25", right="P", strike=560.0, dte=1, bid=3.1, ask=3.4, delta=-0.50),
    ]
    selected = select_contract(chain, right="P", spot=560.2, aggressive_same_day=True, cfg=cfg)
    assert selected is not None
    assert selected.dte == 0


def test_select_contract_uses_delta_fallback_band():
    ts = datetime(2026, 3, 24, 10, 0)
    cfg = ReplayConfig(delta_min=0.45, delta_max=0.55, delta_fallback_min=0.40, delta_fallback_max=0.60)
    chain = [
        _quote(ts, expiry="2026-03-25", right="C", strike=565.0, dte=1, bid=2.5, ask=2.8, delta=0.42),
    ]
    selected = select_contract(chain, right="C", spot=565.0, aggressive_same_day=False, cfg=cfg)
    assert selected is not None
    assert abs(selected.delta) == 0.42


def test_fill_model_is_conservative_between_mid_and_touch():
    ts = datetime(2026, 3, 24, 10, 0)
    cfg = ReplayConfig()
    quote = _quote(ts, expiry="2026-03-25", right="C", strike=565.0, dte=1, bid=2.0, ask=2.4, delta=0.5)
    entry = entry_fill_price(quote, urgent=False, cfg=cfg)
    exit_px = exit_fill_price(quote, reason="TP1_1R", cfg=cfg)
    assert quote.mid <= entry <= quote.ask
    assert quote.bid <= exit_px <= quote.mid


def test_contract_rejection_reason_identifies_spread_filter():
    ts = datetime(2026, 3, 24, 10, 0)
    cfg = ReplayConfig(max_spread_pct=0.05)
    chain = [
        _quote(ts, expiry="2026-03-25", right="C", strike=565.0, dte=1, bid=2.0, ask=3.0, delta=0.50),
    ]
    reason = explain_contract_rejection(chain, right="C", aggressive_same_day=False, cfg=cfg)
    assert reason == "spread_too_wide"


def test_recent_chain_fallback_uses_previous_snapshot():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00"]),
            "symbol": ["QQQ"],
            "open": [500.0],
            "high": [500.2],
            "low": [499.9],
            "close": [500.1],
            "vwap": [500.0],
            "volume": [1000.0],
        }
    )
    prev_ts = datetime(2026, 3, 24, 9, 59)
    cache = {
        (prev_ts, "C"): [_quote(prev_ts, expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.2, delta=0.5)],
    }
    bt = SpyOptionReplayBacktester(bars=bars, option_cache=cache, config=ReplayConfig(chain_lookup_back_min=2))
    chain = bt._chain_for_recent(datetime(2026, 3, 24, 10, 0), "C")
    assert len(chain) == 1
    assert chain[0].ts == prev_ts


def test_quote_mode_auto_falls_back_to_synthetic_when_real_missing():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.1],
            "high": [500.2, 500.3],
            "low": [499.9, 500.0],
            "close": [500.1, 500.2],
            "vwap": [500.0, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    cache, info = load_option_quotes_for_replay(
        "2026-03-24",
        "2026-03-25",
        underlying="QQQ",
        bars=bars,
        quote_mode="synthetic",
    )
    assert cache
    assert info["data_mode"] == "synthetic"
    assert info["confidence"] == "low"
    assert info["synthetic_profile"] == "atm_focused_dte01_smile_v1"


def test_quote_mode_auto_attempts_backfill_before_synthetic(monkeypatch):
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.1],
            "high": [500.2, 500.3],
            "low": [499.9, 500.0],
            "close": [500.1, 500.2],
            "vwap": [500.0, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    real_cache = {
        (datetime(2026, 3, 24, 10, 0), "C"): [
            _quote(
                datetime(2026, 3, 24, 10, 0),
                expiry="2026-03-25",
                right="C",
                strike=500.0,
                dte=1,
                bid=2.0,
                ask=2.2,
                delta=0.5,
            )
        ]
    }
    state = {"calls": 0}

    def fake_load_option_quotes(start_date, end_date, underlying="QQQ"):
        state["calls"] += 1
        if state["calls"] == 1:
            return {}
        return real_cache

    monkeypatch.setattr(market_data, "load_option_quotes", fake_load_option_quotes)
    monkeypatch.setattr(
        market_data,
        "attempt_quotes_backfill",
        lambda **kwargs: {"attempted": True, "mode": "synth", "succeeded": True, "returncode": 0},
    )

    cache, info = load_option_quotes_for_replay(
        "2026-03-24",
        "2026-03-25",
        underlying="QQQ",
        bars=bars,
        quote_mode="auto",
        backfill_mode="synth",
    )
    assert cache == real_cache
    assert info["data_mode"] == "real_backfilled"
    assert info["confidence"] == "high"
    assert info["backfill_attempted"] is True
    assert info["backfill_result"]["succeeded"] is True


def test_detect_setup_for_bull_reject():
    ts = pd.date_range("2026-03-24 09:30:00", periods=220, freq="1min")
    close = pd.Series([500 + i * 0.2 for i in range(220)], dtype=float)
    bars = pd.DataFrame(
        {
            "ts": ts,
            "symbol": "SPY",
            "open": close - 0.05,
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "vwap": close - 0.10,
            "volume": 1_000.0,
        }
    )
    bars.loc[219, "low"] = bars.loc[219, "close"] - 1.1
    bars.loc[219, "open"] = bars.loc[219, "close"] - 0.2
    bars.loc[219, "high"] = bars.loc[219, "close"] + 0.15
    bars.loc[219, "volume"] = 5_000.0
    feats = build_features(bars)
    regime, _ = classify_signal_regime(feats, 219)
    side = detect_setup(feats.iloc[219], ReplayConfig(), regime)
    assert regime in {"FRESH_BULL", "TREND_BULL"}
    assert side == "CALL"


def test_clean_intraday_bars_filters_obvious_price_corruption():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(
                [
                    "2026-03-25 11:38:00",
                    "2026-03-25 11:39:00",
                    "2026-03-25 11:40:00",
                ]
            ),
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "open": [587.9, 588.03, 587.95],
            "high": [588.0, 588.04, 588.02],
            "low": [587.8, 0.06, 587.90],
            "close": [587.95, 5.29, 587.98],
            "vwap": [587.92, 259.10, 587.97],
            "volume": [1000.0, 2000.0, 1100.0],
        }
    )

    cleaned = clean_intraday_bars(bars)

    assert len(cleaned) == 2
    assert cleaned["ts"].dt.strftime("%H:%M:%S").tolist() == ["11:38:00", "11:40:00"]


def test_resolve_indicator_profile_defaults_to_rth_close_sma():
    cfg = ReplayConfig()
    profile = resolve_indicator_profile(cfg)

    assert profile["name"] == "standard_close_rth_sma"
    assert profile["price_source"] == "close"
    assert profile["ma_mode"] == "sma"
    assert profile["session_mode"] == "rth"


def test_build_features_honors_explicit_indicator_profile():
    bars = pd.DataFrame(
        {
            "ts": pd.date_range("2026-03-24 09:30:00", periods=210, freq="1min"),
            "symbol": ["QQQ"] * 210,
            "open": [100.0 + i for i in range(210)],
            "high": [100.5 + i for i in range(210)],
            "low": [99.5 + i for i in range(210)],
            "close": [100.0 + i for i in range(210)],
            "vwap": [100.0 + i for i in range(210)],
            "volume": [1000.0] * 210,
        }
    )
    cfg = ReplayConfig(indicator_profile="standard_close_extended_sma", market_data_session="extended")

    feats = build_features(bars, cfg)

    assert round(float(feats.iloc[199]["ma200"]), 6) == round(sum(100.0 + i for i in range(200)) / 200.0, 6)


def test_load_spy_bars_applies_clean_mode(monkeypatch):
    class _FakeClient:
        def query(self, _q, _params, settings=None):
            return [
                {
                    "ts": "2026-03-25 11:38:00",
                    "symbol": "QQQ",
                    "open": 587.9,
                    "high": 588.0,
                    "low": 587.8,
                    "close": 587.95,
                    "vwap": 587.92,
                    "volume": 1000.0,
                },
                {
                    "ts": "2026-03-25 11:39:00",
                    "symbol": "QQQ",
                    "open": 588.03,
                    "high": 588.04,
                    "low": 0.06,
                    "close": 5.29,
                    "vwap": 259.10,
                    "volume": 2000.0,
                },
            ]

    monkeypatch.setattr(market_data, "get_client", lambda: _FakeClient())

    cleaned = market_data.load_spy_bars("2026-03-25", "2026-03-26", symbol="QQQ", quality_mode="clean")

    assert len(cleaned) == 1
    assert cleaned.iloc[0]["close"] == 587.95


def test_ma10_reject_extension_filter_blocks_late_fresh_entry():
    strategy = get_strategy("ma10_reject_0324")
    df = pd.DataFrame(
        {
            "ma10": [102.0, 101.5, 100.0, 98.0],
            "ma20": [101.5, 101.0, 100.5, 99.0],
            "ma200": [100.0, 100.0, 101.0, 101.0],
            "vwap_day": [101.0, 100.8, 100.1, 99.2],
            "ma10_slope": [0.2, -0.5, -1.5, -0.6],
            "open": [101.8, 101.2, 100.3, 97.6],
            "high": [102.2, 101.4, 100.5, 98.5],
            "low": [101.4, 100.8, 99.7, 96.8],
            "close": [101.9, 101.0, 100.0, 97.0],
            "volume": [120.0, 130.0, 140.0, 220.0],
            "vol_ma20": [100.0, 100.0, 100.0, 100.0],
            "atr_14": [1.0, 1.0, 1.0, 1.0],
            "body": [0.1, 0.2, 0.3, 0.6],
            "range": [0.8, 0.6, 0.8, 1.7],
            "lower_wick": [0.4, 0.2, 0.3, 0.2],
            "upper_wick": [0.3, 0.2, 0.2, 0.9],
            "close_pos": [0.625, 0.333333, 0.375, 0.117647],
        }
    )
    row = df.iloc[3]

    blocked = strategy.detect_signal(
        df,
        3,
        row,
        ReplayConfig(ma10_reject_ext_atr_max=2.2, enable_entry_quality_gate=False),
    )
    allowed = strategy.detect_signal(
        df,
        3,
        row,
        ReplayConfig(ma10_reject_ext_atr_max=4.0, enable_entry_quality_gate=False),
    )
    assert blocked is None
    assert allowed is not None
    assert allowed.signal_kind == "ma10_reject"
    assert allowed.side == "PUT"


def test_entry_quality_gate_blocks_chasing_far_from_ma10():
    strategy = get_strategy("ma10_reject_0324")
    df = pd.DataFrame(
        {
            "ma10": [101.5, 101.8, 102.2],
            "ma20": [101.0, 101.2, 101.4],
            "ma200": [100.0, 100.0, 100.0],
            "vwap_day": [101.0, 101.3, 101.6],
            "ma10_slope": [0.3, 0.3, 0.4],
            "open": [101.3, 101.6, 103.0],
            "high": [101.7, 102.0, 103.5],
            "low": [101.1, 101.4, 101.9],
            "close": [101.6, 101.9, 103.4],
            "volume": [120.0, 130.0, 240.0],
            "vol_ma20": [100.0, 100.0, 100.0],
            "atr_14": [1.0, 1.0, 1.0],
            "body": [0.3, 0.3, 0.4],
            "range": [0.6, 0.6, 1.6],
            "lower_wick": [0.2, 0.2, 1.1],
            "upper_wick": [0.1, 0.1, 0.1],
            "close_pos": [0.833333, 0.833333, 0.9375],
        }
    )
    row = df.iloc[2]
    gated = strategy.detect_signal(df, 2, row, ReplayConfig(enable_entry_quality_gate=True))
    ungated = strategy.detect_signal(df, 2, row, ReplayConfig(enable_entry_quality_gate=False))
    assert gated is None
    assert ungated is not None
    assert ungated.side == "CALL"


def test_daily_best_additive_branch_is_off_by_default():
    strategy = get_strategy("ma10_reject_0324")
    df = pd.DataFrame(
        {
            "ma10": [99.6, 99.8, 100.2, 100.8, 101.2],
            "ma20": [99.8, 100.0, 100.2, 100.5, 100.8],
            "ma200": [100.0, 100.0, 100.0, 100.0, 100.0],
            "vwap_day": [100.0, 100.2, 100.5, 100.8, 101.0],
            "ma10_slope": [0.2, 0.3, 0.3, 0.3, 0.3],
            "open": [99.9, 100.2, 100.7, 101.0, 101.2],
            "high": [100.2, 100.5, 101.0, 101.4, 101.8],
            "low": [99.8, 100.1, 100.6, 100.9, 101.1],
            "close": [100.1, 100.45, 100.95, 101.3, 101.6],
            "volume": [100.0, 110.0, 120.0, 160.0, 180.0],
            "vol_ma20": [100.0, 100.0, 100.0, 100.0, 100.0],
            "atr_14": [0.5, 0.5, 0.5, 0.5, 0.5],
            "body": [0.2, 0.25, 0.25, 0.3, 0.4],
            "range": [0.4, 0.4, 0.4, 0.5, 0.7],
            "lower_wick": [0.1, 0.1, 0.1, 0.1, 0.1],
            "upper_wick": [0.1, 0.1, 0.1, 0.1, 0.1],
            "close_pos": [0.75, 0.75, 0.75, 0.8, 0.714286],
        }
    )
    row = df.iloc[4]
    signal = strategy.detect_signal(
        df,
        4,
        row,
        ReplayConfig(
            enable_daily_best_additive_branch=False,
            enable_fresh_momentum_branch=False,
            volume_mult_min=10.0,
        ),
    )
    assert signal is None


def test_daily_best_additive_branch_can_emit_signal():
    strategy = get_strategy("ma10_reject_0324")
    df = pd.DataFrame(
        {
            "ma10": [99.6, 99.8, 100.2, 100.8, 101.2],
            "ma20": [99.8, 100.0, 100.2, 100.5, 100.8],
            "ma200": [100.0, 100.0, 100.0, 100.0, 100.0],
            "vwap_day": [100.0, 100.2, 100.5, 100.8, 101.0],
            "ma10_slope": [0.2, 0.3, 0.3, 0.3, 0.3],
            "open": [99.9, 100.2, 100.7, 101.0, 101.2],
            "high": [100.2, 100.5, 101.0, 101.4, 101.8],
            "low": [99.8, 100.1, 100.6, 100.9, 101.1],
            "close": [100.1, 100.45, 100.95, 101.3, 101.6],
            "volume": [100.0, 110.0, 120.0, 160.0, 180.0],
            "vol_ma20": [100.0, 100.0, 100.0, 100.0, 100.0],
            "atr_14": [0.5, 0.5, 0.5, 0.5, 0.5],
            "body": [0.2, 0.25, 0.25, 0.3, 0.4],
            "range": [0.4, 0.4, 0.4, 0.5, 0.7],
            "lower_wick": [0.1, 0.1, 0.1, 0.1, 0.1],
            "upper_wick": [0.1, 0.1, 0.1, 0.1, 0.1],
            "close_pos": [0.75, 0.75, 0.75, 0.8, 0.714286],
        }
    )
    row = df.iloc[4]
    signal = strategy.detect_signal(
        df,
        4,
        row,
        ReplayConfig(
            enable_daily_best_additive_branch=True,
            enable_fresh_momentum_branch=False,
            volume_mult_min=10.0,
            daily_best_breakout_lookback=3,
        ),
    )
    assert signal is not None
    assert signal.signal_kind == "daily_best_additive"
    assert signal.side == "CALL"


def test_daily_best_additive_can_be_limited_to_calls():
    strategy = get_strategy("ma10_reject_0324")
    df = pd.DataFrame(
        {
            "ma10": [100.4, 100.2, 99.8, 99.3, 98.9],
            "ma20": [100.2, 100.1, 100.0, 99.8, 99.6],
            "ma200": [100.0, 100.0, 100.0, 100.0, 100.0],
            "vwap_day": [100.0, 99.9, 99.7, 99.4, 99.2],
            "ma10_slope": [-0.2, -0.2, -0.3, -0.3, -0.3],
            "open": [100.2, 100.0, 99.9, 99.6, 99.2],
            "high": [100.5, 100.3, 100.0, 99.7, 99.3],
            "low": [100.0, 99.8, 99.6, 99.2, 98.6],
            "close": [100.1, 99.9, 99.7, 99.3, 98.7],
            "volume": [100.0, 110.0, 120.0, 140.0, 170.0],
            "vol_ma20": [100.0, 100.0, 100.0, 100.0, 100.0],
            "atr_14": [0.5, 0.5, 0.5, 0.5, 0.5],
            "body": [0.1, 0.1, 0.2, 0.3, 0.5],
            "range": [0.5, 0.5, 0.4, 0.5, 0.7],
            "lower_wick": [0.1, 0.1, 0.1, 0.1, 0.1],
            "upper_wick": [0.1, 0.1, 0.1, 0.1, 0.1],
            "close_pos": [0.2, 0.2, 0.25, 0.2, 0.142857],
        }
    )
    row = df.iloc[4]
    allowed = strategy.detect_signal(
        df,
        4,
        row,
        ReplayConfig(
            enable_daily_best_additive_branch=True,
            enable_fresh_momentum_branch=False,
            volume_mult_min=10.0,
            daily_best_breakout_lookback=3,
            daily_best_call_only=False,
        ),
    )
    blocked = strategy.detect_signal(
        df,
        4,
        row,
        ReplayConfig(
            enable_daily_best_additive_branch=True,
            enable_fresh_momentum_branch=False,
            volume_mult_min=10.0,
            daily_best_breakout_lookback=3,
            daily_best_call_only=True,
        ),
    )
    assert allowed is not None
    assert allowed.signal_kind == "daily_best_additive"
    assert allowed.side == "PUT"
    assert blocked is None


def test_missing_exit_quote_uses_fallback_instead_of_dropping_position():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 15:29:00", "2026-03-24 15:30:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.2],
            "high": [500.3, 500.4],
            "low": [499.8, 500.0],
            "close": [500.2, 500.1],
            "vwap": [500.1, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(
        bars=bars,
        option_cache={},
        config=ReplayConfig(final_flatten="15:30"),
    )
    bt.position = PositionState(
        entry_order_id="entry_1",
        stop_order_id="stop_1",
        tp_order_id="tp_1",
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        entry_ts=datetime(2026, 3, 24, 15, 29),
        entry_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        entry_underlying=500.0,
        entry_option=2.0,
        entry_delta=0.5,
        contracts=1,
        remaining=1,
        structure_stop=499.0,
        r_per_share=1.0,
        target_1r=501.0,
        target_1p2r=501.2,
        last_quote=_quote(datetime(2026, 3, 24, 15, 29), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=1.8, ask=2.2, delta=0.5),
    )
    bt.run()
    assert bt.position is None
    assert any(t["reason"] == "EOD_FLATTEN_STALE_QUOTE" for t in bt.trades)


def test_pending_entry_allows_small_adaptive_chase():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.1],
            "high": [500.3, 500.4],
            "low": [499.9, 500.0],
            "close": [500.2, 500.2],
            "vwap": [500.0, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig())
    bt.pending_entry = PendingEntry(
        order_id="entry_1",
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        created_ts=datetime(2026, 3, 24, 10, 0),
        trigger_ts=datetime(2026, 3, 24, 10, 0),
        trigger_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        trigger_underlying=500.2,
        limit_price=2.10,
        entry_delta=0.50,
        structure_stop=499.0,
        r_per_share=1.2,
        target_1r=501.4,
        target_1p2r=501.64,
        contracts=1,
    )
    q = _quote(datetime(2026, 3, 24, 10, 1), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.15, delta=0.5)
    bt.last_quote_by_contract[(q.expiry, q.strike, "C")] = q
    bt.option_cache[(datetime(2026, 3, 24, 10, 1), "C")] = [q]
    bt._try_fill_pending_entry(1, bars.iloc[1])
    assert bt.position is not None


def test_pending_entry_can_fill_on_second_attempt_and_writes_audit():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00", "2026-03-24 10:02:00"]),
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "open": [500.0, 500.1, 500.2],
            "high": [500.3, 500.4, 500.5],
            "low": [499.9, 500.0, 500.1],
            "close": [500.2, 500.2, 500.3],
            "vwap": [500.0, 500.1, 500.2],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig(pending_entry_timeout_bars=2))
    bt.pending_entry = PendingEntry(
        order_id="entry_1",
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        created_ts=datetime(2026, 3, 24, 10, 0),
        trigger_ts=datetime(2026, 3, 24, 10, 0),
        trigger_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        trigger_underlying=500.2,
        limit_price=2.10,
        entry_delta=0.50,
        structure_stop=499.0,
        r_per_share=1.2,
        target_1r=501.4,
        target_1p2r=501.64,
        contracts=1,
    )
    q1 = _quote(datetime(2026, 3, 24, 10, 1), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.20, delta=0.5)
    q2 = _quote(datetime(2026, 3, 24, 10, 2), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.17, delta=0.5)
    bt.option_cache[(datetime(2026, 3, 24, 10, 1), "C")] = [q1]
    bt.option_cache[(datetime(2026, 3, 24, 10, 2), "C")] = [q2]
    bt._try_fill_pending_entry(1, bars.iloc[1])
    assert bt.position is None
    bt._try_fill_pending_entry(2, bars.iloc[2])
    assert bt.position is not None
    statuses = [row["status"] for row in bt.entry_audit]
    assert "missed" in statuses
    assert "filled" in statuses


def test_run_result_includes_orders_fills_and_cash_ledger():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 15:29:00", "2026-03-24 15:30:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.2],
            "high": [500.3, 500.4],
            "low": [499.8, 500.0],
            "close": [500.2, 500.1],
            "vwap": [500.1, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig())
    bt.position = PositionState(
        entry_order_id="entry_1",
        stop_order_id="stop_1",
        tp_order_id="tp_1",
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        entry_ts=datetime(2026, 3, 24, 15, 29),
        entry_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        entry_underlying=500.0,
        entry_option=2.0,
        entry_delta=0.5,
        contracts=1,
        remaining=1,
        structure_stop=499.0,
        r_per_share=1.0,
        target_1r=501.0,
        target_1p2r=501.2,
        last_quote=_quote(datetime(2026, 3, 24, 15, 29), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=1.8, ask=2.2, delta=0.5),
    )
    result = bt.run()
    assert "orders" in result
    assert "fills" in result
    assert "cash_ledger" in result
    assert "bracket_audit" in result
    assert not result["orders"].empty
    assert not result["fills"].empty
    assert not result["cash_ledger"].empty
    assert not result["bracket_audit"].empty


def test_selection_audit_records_rejection_reason():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.1],
            "high": [500.2, 500.3],
            "low": [499.9, 500.0],
            "close": [500.1, 500.2],
            "vwap": [500.0, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig())
    setup = bt.strategy.build_pending_setup(
        bt.strategy.detect_signal(bt.df, 1, bt.df.iloc[1], bt.cfg) or __import__("services.backtest.spy_option_0324_replay", fromlist=["StrategySignal"]).StrategySignal(
            side="CALL", regime="TREND_BULL", grade="B", high=500.3, low=500.0, atr=1.0
        ),
        datetime(2026, 3, 24, 10, 1),
        1,
        bt.df.iloc[1],
    )
    bt._create_pending_entry(1, bt.df.iloc[1], setup)
    assert not pd.DataFrame(bt.selection_audit).empty
    assert bt.selection_audit[-1]["selected"] is False
    assert bt.selection_audit[-1]["rejection_reason"] in {"no_chain_snapshot", "invalid_quote", "delta_out_of_range", "spread_too_wide", "oi_too_low", "dte_mismatch"}


def test_run_result_includes_normalized_selection_candidates():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00"]),
            "symbol": ["QQQ", "QQQ"],
            "open": [500.0, 500.1],
            "high": [500.2, 500.3],
            "low": [499.9, 500.0],
            "close": [500.1, 500.2],
            "vwap": [500.0, 500.1],
            "volume": [1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig())
    setup = bt.strategy.build_pending_setup(
        __import__("services.backtest.spy_option_0324_replay", fromlist=["StrategySignal"]).StrategySignal(
            side="CALL", regime="TREND_BULL", grade="B", high=500.3, low=500.0, atr=1.0
        ),
        datetime(2026, 3, 24, 10, 1),
        1,
        bt.df.iloc[1],
    )
    bt._create_pending_entry(1, bt.df.iloc[1], setup)
    result = bt.run()
    assert "selection_candidates" in result
    assert isinstance(result["selection_candidates"], pd.DataFrame)


def test_second_strategy_plugin_can_run_through_engine():
    bars = pd.DataFrame(
        {
            "ts": pd.date_range("2026-03-24 09:30:00", periods=20, freq="1min"),
            "symbol": ["QQQ"] * 20,
            "open": [500.0 + i * 0.05 for i in range(20)],
            "high": [500.1 + i * 0.05 for i in range(20)],
            "low": [499.9 + i * 0.05 for i in range(20)],
            "close": [500.05 + i * 0.05 for i in range(20)],
            "vwap": [500.0 + i * 0.04 for i in range(20)],
            "volume": [1000.0] * 20,
        }
    )
    bt = SpyOptionReplayBacktester(
        bars=bars,
        option_cache={},
        config=ReplayConfig(),
        strategy=OpeningRangeBreakoutStrategy(),
    )
    result = bt.run()
    assert "summary" in result
    assert "selection_audit" in result


def test_strategy_registry_exposes_default_and_orb():
    names = list_strategies()
    assert "ma10_reject_0324" in names
    assert "ma10_reject_0324_trend_cont_exp" in names
    assert "ma10_reject_0324_trend_cont_put_exp" in names
    assert "orb" in names
    assert "vwap" in names
    assert get_strategy("ma10_reject_0324").__class__.__name__ == "MA10Reject0324Strategy"
    assert (
        get_strategy("ma10_reject_0324_trend_cont_exp").__class__.__name__
        == "MA10Reject0324TrendContinuationExperimentalStrategy"
    )
    assert (
        get_strategy("ma10_reject_0324_trend_cont_put_exp").__class__.__name__
        == "MA10Reject0324TrendContinuationPutExperimentalStrategy"
    )
    assert get_strategy("orb").__class__.__name__ == "OpeningRangeBreakoutStrategy"
    assert get_strategy("vwap").__class__.__name__ == "VWAPReclaimContinuationStrategy"


def test_trend_continuation_branch_is_isolated_from_baseline_strategy():
    df = pd.DataFrame(
        [
            {
                "ts": pd.Timestamp("2026-03-24 11:09:00"),
                "open": 481.10,
                "high": 481.15,
                "low": 480.82,
                "close": 480.90,
                "ma10": 481.30,
                "ma20": 481.60,
                "ma200": 483.50,
                "ma10_slope": -0.18,
                "vwap_day": 481.00,
                "volume": 1000.0,
                "vol_ma20": 1000.0,
                "close_pos": 0.24,
                "body": 0.20,
                "range": 0.33,
                "atr_14": 0.55,
                "upper_wick": 0.05,
                "lower_wick": 0.08,
            },
            {
                "ts": pd.Timestamp("2026-03-24 11:10:00"),
                "open": 480.92,
                "high": 480.96,
                "low": 480.70,
                "close": 480.74,
                "ma10": 481.08,
                "ma20": 481.45,
                "ma200": 483.40,
                "ma10_slope": -0.19,
                "vwap_day": 480.90,
                "volume": 980.0,
                "vol_ma20": 1000.0,
                "close_pos": 0.15,
                "body": 0.18,
                "range": 0.26,
                "atr_14": 0.54,
                "upper_wick": 0.04,
                "lower_wick": 0.04,
            },
            {
                "ts": pd.Timestamp("2026-03-24 11:11:00"),
                "open": 480.72,
                "high": 480.78,
                "low": 480.34,
                "close": 480.40,
                "ma10": 480.95,
                "ma20": 481.31,
                "ma200": 483.30,
                "ma10_slope": -0.22,
                "vwap_day": 480.70,
                "volume": 1400.0,
                "vol_ma20": 1000.0,
                "close_pos": 0.14,
                "body": 0.32,
                "range": 0.44,
                "atr_14": 0.58,
                "upper_wick": 0.06,
                "lower_wick": 0.06,
            },
        ]
    )
    row = df.iloc[2]
    cfg = ReplayConfig(enable_trend_continuation_branch=True, trend_cont_breakout_lookback=2, volume_mult_min=10.0)

    baseline = get_strategy("ma10_reject_0324")
    experimental = get_strategy("ma10_reject_0324_trend_cont_exp")

    baseline_signal = baseline.detect_signal(df, 2, row, cfg)
    experimental_signal = experimental.detect_signal(df, 2, row, cfg)

    assert baseline_signal is None
    assert experimental_signal is not None
    assert experimental_signal.side == "PUT"
    assert experimental_signal.signal_kind == "trend_continuation"


def test_summary_reports_round_trip_metrics_separately_from_exit_legs():
    engine = SimpleNamespace(
        trades=[
            {"entry_order_id": "entry_1", "pnl": 50.0},
            {"entry_order_id": "entry_1", "pnl": 25.0},
            {"entry_order_id": "entry_2", "pnl": -40.0},
        ],
        equity_curve=[],
        selection_audit=[],
        trade_start_date=None,
        df=pd.DataFrame({"date": pd.to_datetime(["2026-03-24"]).date}),
        skips=[],
        entry_audit=[],
        bracket_audit=[],
        orders=[],
        fills=[],
        cash_ledger=[],
        cash=100000.0,
        cfg=ReplayConfig(),
    )
    result = build_run_result(engine)
    summary = result["summary"]
    assert summary["trades"] == 3
    assert summary["exit_legs"] == 3
    assert summary["entry_count"] == 2
    assert summary["win_rate"] == 2 / 3
    assert summary["round_trip_win_rate"] == 0.5
    assert summary["realized_pnl"] == 35.0
    assert summary["avg_pnl_per_entry"] == 17.5
    assert summary["avg_legs_per_entry"] == 1.5


def test_entry_fill_creates_bracket_children_and_exit_cancels_peer():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00", "2026-03-24 10:02:00"]),
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "open": [500.0, 500.1, 500.2],
            "high": [500.3, 500.4, 501.0],
            "low": [499.9, 500.0, 500.1],
            "close": [500.2, 500.2, 501.0],
            "vwap": [500.0, 500.1, 500.2],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig())
    bt.pending_entry = PendingEntry(
        order_id="entry_1",
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        created_ts=datetime(2026, 3, 24, 10, 0),
        trigger_ts=datetime(2026, 3, 24, 10, 0),
        trigger_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        trigger_underlying=500.2,
        limit_price=2.10,
        entry_delta=0.50,
        structure_stop=499.0,
        r_per_share=1.2,
        target_1r=501.4,
        target_1p2r=501.64,
        contracts=1,
    )
    q1 = _quote(datetime(2026, 3, 24, 10, 1), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.15, delta=0.5)
    q2 = _quote(datetime(2026, 3, 24, 10, 2), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.4, ask=2.6, delta=0.5)
    bt.option_cache[(datetime(2026, 3, 24, 10, 1), "C")] = [q1]
    bt.option_cache[(datetime(2026, 3, 24, 10, 2), "C")] = [q2]
    bt._try_fill_pending_entry(1, bars.iloc[1])
    assert bt.position is not None
    assert bt.position.stop_order_id is not None
    assert bt.position.tp_order_id is not None
    bt._close_position(datetime(2026, 3, 24, 10, 2), q2, 501.0, 1, "TP_1P2R")
    assert any(row["event"] == "CHILDREN_CREATED" for row in bt.bracket_audit)
    assert any(row["event"] == "CHILD_TRIGGERED" for row in bt.bracket_audit)
    cancelled = [row for row in bt.orders if row["status"] == "CANCELLED"]
    assert cancelled


def test_partial_take_profit_syncs_child_quantities():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00"]),
            "symbol": ["QQQ"],
            "open": [500.0],
            "high": [500.5],
            "low": [499.9],
            "close": [500.4],
            "vwap": [500.1],
            "volume": [1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(bars=bars, option_cache={}, config=ReplayConfig())
    bt.position = PositionState(
        entry_order_id="entry_1",
        side="CALL",
        regime="TREND_BULL",
        grade="A",
        entry_ts=datetime(2026, 3, 24, 10, 0),
        entry_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        entry_underlying=500.0,
        entry_option=2.0,
        entry_delta=0.5,
        contracts=2,
        remaining=2,
        structure_stop=499.0,
        r_per_share=1.0,
        target_1r=501.0,
        target_1p2r=501.2,
        stop_order_id="stop_1",
        tp_order_id="tp_1",
        last_quote=_quote(datetime(2026, 3, 24, 10, 0), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.2, delta=0.5),
    )
    bt._close_position(datetime(2026, 3, 24, 10, 0), bt.position.last_quote, 501.0, 1, "TP1_1R")
    assert bt.position is not None
    assert bt.position.remaining == 1
    assert any(row["event"] == "CHILD_QTY_SYNC" for row in bt.bracket_audit)
    replaced = [row for row in bt.orders if row["status"] == "REPLACED"]
    assert replaced


def test_partial_entry_fill_skeleton_only_activates_when_enabled():
    bars = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-03-24 10:00:00", "2026-03-24 10:01:00", "2026-03-24 10:02:00"]),
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "open": [500.0, 500.1, 500.2],
            "high": [500.3, 500.4, 500.5],
            "low": [499.9, 500.0, 500.1],
            "close": [500.2, 500.2, 500.3],
            "vwap": [500.0, 500.1, 500.2],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )
    bt = SpyOptionReplayBacktester(
        bars=bars,
        option_cache={},
        config=ReplayConfig(allow_partial_fills=True, max_fill_contracts_per_event=1, pending_entry_timeout_bars=3),
    )
    bt.pending_entry = PendingEntry(
        order_id="entry_1",
        side="CALL",
        regime="TREND_BULL",
        grade="B",
        created_ts=datetime(2026, 3, 24, 10, 0),
        trigger_ts=datetime(2026, 3, 24, 10, 0),
        trigger_idx=0,
        expiry="2026-03-25",
        strike=500.0,
        trigger_underlying=500.2,
        limit_price=2.10,
        entry_delta=0.50,
        structure_stop=499.0,
        r_per_share=1.2,
        target_1r=501.4,
        target_1p2r=501.64,
        contracts=2,
    )
    q1 = _quote(datetime(2026, 3, 24, 10, 1), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.15, delta=0.5)
    q2 = _quote(datetime(2026, 3, 24, 10, 2), expiry="2026-03-25", right="C", strike=500.0, dte=1, bid=2.0, ask=2.15, delta=0.5)
    bt.option_cache[(datetime(2026, 3, 24, 10, 1), "C")] = [q1]
    bt.option_cache[(datetime(2026, 3, 24, 10, 2), "C")] = [q2]
    bt._try_fill_pending_entry(1, bars.iloc[1])
    assert bt.position is not None
    assert bt.position.contracts == 1
    assert bt.pending_entry is not None
    bt._try_fill_pending_entry(2, bars.iloc[2])
    assert bt.pending_entry is None
    assert bt.position is not None
    assert bt.position.contracts == 2
    statuses = [row["status"] for row in bt.entry_audit]
    assert "partial_filled" in statuses
