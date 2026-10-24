from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import yaml

from services.option_replay.core.types import PendingSetup
from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.options_plugins.ma_1dte_reject_shared import (
    build_replay_cfg_from_extras,
    ensure_et_aware,
    select_option_quote_for_underlying,
)
from strategies.options_plugins.qqq_1dte_ma10_reject import QQQ1DTEMa10RejectPlugin
from strategies.options_plugins.spy_1dte_ma10_reject import SPY1DTEMa10RejectPlugin


RUNNER_STRONG_EXPECTED = {
    "enable_trend_continuation_branch": True,
    "continuation_pending_entry_timeout_bars": 2,
    "continuation_dynamic_reprice_to_ask": True,
    "continuation_max_premium_expansion_pct": 0.08,
    "trend_cont_put_consecutive_bars_below_ma10": 2,
    "put_close_pos_max_for_entry": 0.65,
    "trend_cont_put_max_high_over_ma10_atr": 0.2,
    "confirm_window_bars": 2,
    "confirm_decay_from_bar": 99,
    "ma10_reject_ext_atr_max": 2.2,
}

QQQ_DUAL_DEFAULTS_EXPECTED = {
    "enable_momentum_no_retest_dual": True,
    "momentum_breakout_lookback_bars": 5,
    "momentum_higher_low_bars": 3,
    "momentum_lower_high_bars": 3,
    "momentum_ma10_slope_min_abs": 0.02,
    "momentum_close_pos_min_call": 0.70,
    "momentum_close_pos_max_put": 0.30,
    "momentum_body_frac_min": 0.50,
    "momentum_vol_ratio_min": 0.85,
    "momentum_dist_ma10_atr_max": 2.2,
}

SPY_GRAPH_PRIORITY_EXPECTED = {
    "enable_trend_continuation_branch": False,
    "continuation_pending_entry_timeout_bars": 2,
    "continuation_dynamic_reprice_to_ask": True,
    "continuation_max_premium_expansion_pct": 0.08,
    "confirm_window_bars": 2,
    "confirm_decay_from_bar": 99,
    "ma10_reject_ext_atr_max": 2.2,
    "cross_retest_lookback_bars": 20,
    "retest_window_bars": 3,
    "retest_touch_tolerance_abs": 0.15,
    "enable_vwap_as_soft_filter": False,
    "enable_momentum_no_retest_dual": True,
    "momentum_breakout_lookback_bars": 5,
    "momentum_higher_low_bars": 3,
    "momentum_lower_high_bars": 3,
    "momentum_ma10_slope_min_abs": 0.02,
    "momentum_close_pos_min_call": 0.70,
    "momentum_close_pos_max_put": 0.30,
    "momentum_body_frac_min": 0.50,
    "momentum_vol_ratio_min": 0.85,
    "momentum_dist_ma10_atr_max": 2.2,
}


def _load_strategy(strategy_id: str) -> dict:
    cfg = yaml.safe_load(Path("./config.yml").read_text())
    for strategy in cfg.get("strategies", []):
        if strategy.get("id") == strategy_id:
            return strategy
    raise AssertionError(f"strategy not found: {strategy_id}")


def test_ma_strategies_use_pure_plugin_runner_profile():
    strategy = _load_strategy("qqq_1dte_ma10_reject_0324_v2_001")
    extras = ((strategy.get("params") or {}).get("extras") or {})
    assert extras.get("kernel_mode") == "pure_plugin"

    spy_strategy = _load_strategy("spy_1dte_ma10_reject_0324_001")
    spy_extras = SPY1DTEMa10RejectPlugin().apply_default_extras(
        dict(((spy_strategy.get("params") or {}).get("extras") or {}))
    )
    assert spy_extras.get("kernel_mode") == "pure_plugin"
    for key, expected in SPY_GRAPH_PRIORITY_EXPECTED.items():
        assert spy_extras.get(key) == expected


def test_ma_plugins_default_to_runner_profile_and_replay_cfg():
    qqq_extras = QQQ1DTEMa10RejectPlugin().apply_default_extras({})
    qqq_cfg = build_replay_cfg_from_extras(qqq_extras)
    assert qqq_cfg.enable_trend_continuation_branch is True
    assert qqq_cfg.continuation_pending_entry_timeout_bars == 2
    assert qqq_cfg.continuation_dynamic_reprice_to_ask is True
    assert qqq_cfg.continuation_max_premium_expansion_pct == 0.08
    assert qqq_cfg.trend_cont_put_consecutive_bars_below_ma10 == 2
    assert qqq_cfg.put_close_pos_max_for_entry == 0.65
    assert qqq_cfg.trend_cont_put_max_high_over_ma10_atr == 0.2
    assert qqq_cfg.confirm_window_bars == 2
    assert qqq_cfg.confirm_decay_from_bar == 99
    assert qqq_cfg.ma10_reject_ext_atr_max == 2.2
    for key, expected in QQQ_DUAL_DEFAULTS_EXPECTED.items():
        assert qqq_extras[key] == expected

    spy_extras = SPY1DTEMa10RejectPlugin().apply_default_extras({})
    spy_cfg = build_replay_cfg_from_extras(spy_extras)
    assert spy_cfg.enable_trend_continuation_branch is False
    assert spy_cfg.continuation_pending_entry_timeout_bars == 2
    assert spy_cfg.continuation_dynamic_reprice_to_ask is True
    assert spy_cfg.continuation_max_premium_expansion_pct == 0.08
    assert spy_cfg.confirm_window_bars == 2
    assert spy_cfg.confirm_decay_from_bar == 99
    assert spy_cfg.ma10_reject_ext_atr_max == 2.2
    assert spy_extras["cross_retest_lookback_bars"] == 20
    assert spy_extras["retest_window_bars"] == 3
    assert spy_extras["retest_touch_tolerance_abs"] == 0.15
    assert spy_extras["enable_momentum_no_retest_dual"] is False


def test_build_signal_pure_expires_pending_by_timestamp_not_rolling_index(monkeypatch):
    plugin = QQQ1DTEMa10RejectPlugin()
    plugin.apply_default_extras({"confirm_window_bars": 2})
    plugin._pure_strategy_id = "qqq_1dte_ma10_reject_0324_v2_001"
    plugin._pure_underlying = "QQQ"
    plugin._pure_last_day = datetime(2026, 3, 30).date()
    setup_ts = datetime(2026, 3, 30, 10, 0)
    pending = PendingSetup(
        side="PUT",
        regime="TREND_BEAR",
        grade="A",
        setup_ts=setup_ts,
        setup_idx=291,
        reject_number=None,
        high=565.0,
        low=564.0,
        atr=0.5,
        volume_ratio=1.2,
        signal_kind="ma10_reject",
    )

    row_ts = setup_ts + timedelta(minutes=3)
    history = [{"ts": row_ts - timedelta(minutes=i)} for i in range(291, -1, -1)]
    frame = pd.DataFrame(
        [
            {
                "ts": bar["ts"],
                "close": 563.0,
                "high": 563.2,
                "low": 562.8,
                "ma10": 563.5,
                "ma20": 564.0,
                "ma200": 565.0,
                "vwap_day": 564.0,
                "atr_14": 0.5,
                "volume": 1000.0,
                "vol_ma20": 900.0,
            }
            for bar in history
        ]
    )

    monkeypatch.setattr(
        "strategies.options_plugins.qqq_1dte_ma10_reject.build_feature_frame_from_history",
        lambda symbol, bars, extras=None: frame,
    )
    plugin._pure_replay_strategy = SimpleNamespace(on_new_day=lambda: None)
    signal_module = plugin._resolve_strategy_definition().signal_module
    signal_module._last_day = row_ts.date()
    signal_module._pending_setup = pending
    observed = {}

    def _capture(runtime, **kwargs):
        observed["reason"] = kwargs.get("reason")

    monkeypatch.setattr(plugin, "_emit_minute_observability", _capture)
    runtime = PluginRuntimeState(
        strategy_id="qqq_1dte_ma10_reject_0324_v2_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=row_ts,
        wall_now=row_ts,
        spot=563.0,
        bar=KernelBarContext(ts=row_ts, symbol="QQQ", close=563.0),
        payload={"bar_history": history},
    )

    out = plugin.build_signal_pure(runtime)

    assert out is None
    assert observed["reason"] == "CONFIRM_WINDOW_EXPIRED"
    assert plugin._pure_pending_setup is None


def test_spy_build_signal_pure_advances_confirm_by_setup_timestamp(monkeypatch):
    plugin = SPY1DTEMa10RejectPlugin()
    plugin.apply_default_extras({"confirm_window_bars": 2})
    plugin._pure_strategy_id = "spy_1dte_ma10_reject_0324_001"
    plugin._pure_underlying = "SPY"
    plugin._pure_last_day = datetime(2026, 3, 30).date()
    setup_ts = datetime(2026, 3, 30, 13, 6)
    plugin._pure_pending_setup = PendingSetup(
        side="PUT",
        regime="TREND_BEAR",
        grade="A",
        setup_ts=setup_ts,
        setup_idx=291,
        reject_number=None,
        high=635.5,
        low=634.8,
        atr=0.5,
        volume_ratio=1.2,
        signal_kind="breakdown_cont_put",
    )

    row_ts = setup_ts + timedelta(minutes=1)
    history = [{"ts": row_ts - timedelta(minutes=i)} for i in range(291, -1, -1)]
    frame = pd.DataFrame(
        [
            {
                "ts": bar["ts"],
                "open": 635.0,
                "close": 634.9,
                "high": 635.2,
                "low": 634.85,
                "ma10": 635.1,
                "ma20": 635.6,
                "ma200": 636.4,
                "vwap_day": 636.0,
                "atr_14": 0.5,
                "volume": 1200.0,
                "vol_ma20": 1000.0,
            }
            for bar in history
        ]
    )

    monkeypatch.setattr(
        "strategies.options_plugins.spy_1dte_ma10_reject.build_feature_frame_from_history",
        lambda symbol, bars, extras=None: frame,
    )
    monkeypatch.setattr(
        "strategies.options_kernel.modules.spy_dual_signal.build_feature_frame_from_history",
        lambda symbol, bars, extras=None: frame,
    )
    plugin.apply_default_extras({})

    observed = {}

    def _capture(runtime, **kwargs):
        observed["reason"] = kwargs.get("reason")
        observed["gate_reason"] = kwargs.get("gate_reason")

    monkeypatch.setattr(plugin, "_emit_minute_observability", _capture)
    monkeypatch.setattr(plugin, "_emit_execution_audit", lambda *_args, **_kwargs: None)
    signal_module = plugin._resolve_strategy_definition().signal_module
    signal_module._last_day = row_ts.date()
    signal_module._pending_setup = PendingSetup(
        side="PUT",
        regime="TREND_BEAR",
        grade="A",
        setup_ts=setup_ts,
        setup_idx=291,
        reject_number=None,
        high=635.5,
        low=634.8,
        atr=0.5,
        volume_ratio=1.2,
        signal_kind="breakdown_cont_put",
    )
    signal_module._entry_plan = None

    runtime = PluginRuntimeState(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        kernel_mode="pure_plugin",
        symbol="SPY",
        now=row_ts,
        wall_now=row_ts,
        spot=634.9,
        bar=KernelBarContext(ts=row_ts, symbol="SPY", close=634.9),
        payload={"bar_history": history},
    )

    out = plugin.build_signal_pure(runtime)

    assert out is None
    assert observed["reason"] == "CONFIRM_NOT_MET"
    assert observed["gate_reason"] == "CONFIRM_NOT_MET"
    assert plugin._pure_pending_setup is not None


def test_ensure_et_aware_localizes_and_converts():
    naive = datetime(2026, 3, 30, 9, 41)
    localized = ensure_et_aware(naive)
    assert localized.tzinfo is not None
    assert localized.tzinfo.key == "America/New_York"

    utc = pd.Timestamp("2026-03-30 13:41:00+00:00").to_pydatetime()
    converted = ensure_et_aware(utc)
    assert converted.hour == 9
    assert converted.minute == 41
    assert converted.tzinfo.key == "America/New_York"


def test_select_option_quote_for_underlying_accepts_naive_now(monkeypatch):
    captured = {}

    def _fake_query_option_rows(*, ts_lb, ts_now, **_kwargs):
        captured["ts_now"] = ts_now
        captured["ts_lb"] = ts_lb
        return [
            {
                "ts": pd.Timestamp("2026-03-30 09:41:00-04:00").to_pydatetime(),
                "created_at": pd.Timestamp("2026-03-30 09:41:15-04:00").to_pydatetime(),
                "expiry": "2026-03-31",
                "strike": 565.0,
                "right": "P",
                "dte": 1,
                "bid": 1.2,
                "ask": 1.3,
                "mid": 1.25,
                "iv": 0.2,
                "delta": -0.5,
                "gamma": 0.1,
                "theta": -0.05,
                "vega": 0.03,
                "open_interest": 0,
            }
        ]

    monkeypatch.setattr("strategies.options_plugins.ma_1dte_reject_shared.query_option_rows", _fake_query_option_rows)
    monkeypatch.setattr(
        "strategies.options_plugins.ma_1dte_reject_shared.select_contract",
        lambda chain, **_kwargs: chain[0],
    )

    quote, reason = select_option_quote_for_underlying(
        underlying="QQQ",
        extras={"dte_preferred": 1, "dte_fallback_max": 2, "min_open_interest": 0},
        now=datetime(2026, 3, 30, 9, 41),
        side="PUT",
        spot=564.16,
        spread_max_pct=0.12,
    )

    assert reason is None
    assert quote is not None
    assert quote["opt_symbol"].startswith("OPT:QQQ:")
    assert captured["ts_now"].tzinfo is not None
    assert captured["ts_lb"].tzinfo is not None
