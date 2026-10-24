from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from services.option_replay.core.types import ReplayConfig
from services.option_replay.strategies.ma_cross_stack_continuation_v2_dual_exp import (
    MACrossStackContinuationV2DualExperimentalStrategy,
)
from strategies.options_plugins.ma_1dte_reject_shared import build_replay_cfg_from_extras


def _base_rows(count: int = 211) -> pd.DataFrame:
    start = datetime(2026, 4, 1, 9, 30)
    rows = []
    for i in range(count):
        ts = start + timedelta(minutes=i)
        rows.append(
            {
                "ts": ts,
                "open": 658.0,
                "high": 658.2,
                "low": 657.8,
                "close": 658.0,
                "ma10": 658.0,
                "ma200": 657.9,
                "vwap_day": 657.95,
                "atr_14": 0.25,
                "close_pos": 0.5,
                "body": 0.1,
                "range": 0.4,
                "volume": 1000.0,
                "vol_ma20": 1000.0,
                "ma10_slope": 0.0,
            }
        )
    df = pd.DataFrame(rows)
    return df


def _put_cross_retest_frame() -> tuple[pd.DataFrame, int]:
    df = _base_rows()
    # Recent bearish cross then retest failure near MA10.
    df.loc[207, ["ma10", "ma200", "open", "high", "low", "close"]] = [657.82, 657.78, 657.9, 658.0, 657.7, 657.8]
    df.loc[208, ["ma10", "ma200", "open", "high", "low", "close"]] = [657.65, 657.66, 657.75, 657.9, 657.55, 657.6]
    df.loc[209, ["ma10", "ma200", "open", "high", "low", "close"]] = [657.52, 657.60, 657.55, 657.34, 656.95, 657.05]
    df.loc[209, ["close_pos", "body", "range", "volume", "vol_ma20", "ma10_slope"]] = [0.18, 0.45, 0.65, 1800.0, 1100.0, -0.13]
    df.loc[210, ["ma10", "ma200", "open", "high", "low", "close"]] = [657.40, 657.58, 657.18, 657.28, 656.55, 656.72]
    df.loc[210, ["vwap_day", "atr_14", "close_pos", "body", "range", "volume", "vol_ma20", "ma10_slope"]] = [
        656.50,
        0.09,
        0.23,
        0.46,
        0.73,
        1900.0,
        1100.0,
        -0.12,
    ]
    return df, 210


def test_build_replay_cfg_uses_live_indicator_profile() -> None:
    cfg = build_replay_cfg_from_extras({"market_data_profile_id": "standard_close_extended_sma"})
    assert cfg.indicator_profile == "standard_close_extended_sma"
    assert cfg.market_data_session == "extended"


def test_spy_put_cross_retest_signal_triggers_without_vwap_hard_gate() -> None:
    df, idx = _put_cross_retest_frame()
    strategy = MACrossStackContinuationV2DualExperimentalStrategy()
    strategy.enable_vwap_as_soft_filter = False
    row = df.iloc[idx]
    signal = strategy.detect_signal(df, idx, row, ReplayConfig())
    assert signal is not None
    assert signal.side == "PUT"
    assert signal.regime == "STACK_BEAR_RETEST_V2"


def test_spy_put_cross_retest_ignores_large_dist_ma10_atr_for_setup() -> None:
    df, idx = _put_cross_retest_frame()
    strategy = MACrossStackContinuationV2DualExperimentalStrategy()
    strategy.enable_vwap_as_soft_filter = False
    row = df.iloc[idx]
    setup = strategy.describe_setup(df, idx, row)
    assert setup["dist_ma10_atr"] > 1.8
    assert setup["put_trigger"] == "cross_retest_put"


def _call_momentum_frame() -> tuple[pd.DataFrame, int]:
    df = _base_rows()
    df.loc[205, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [657.60, 658.05, 657.95, 657.70, 657.85, 657.70, 657.75, 0.03]
    df.loc[206, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [657.78, 658.15, 658.05, 657.85, 657.95, 657.72, 657.80, 0.03]
    df.loc[207, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [657.98, 658.22, 658.12, 658.00, 658.04, 657.75, 657.90, 0.03]
    df.loc[208, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [658.10, 658.28, 658.18, 658.08, 658.12, 657.80, 657.96, 0.03]
    df.loc[209, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [658.18, 658.32, 658.26, 658.18, 658.20, 657.85, 658.02, 0.03]
    df.loc[210, ["open", "high", "low", "close", "ma10", "ma200", "vwap_day", "atr_14", "close_pos", "body", "range", "volume", "vol_ma20", "ma10_slope"]] = [
        658.20,
        658.55,
        658.22,
        658.52,
        658.28,
        657.92,
        658.05,
        0.11,
        0.91,
        0.32,
        0.33,
        1700.0,
        1200.0,
        0.04,
    ]
    return df, 210


def _put_momentum_frame() -> tuple[pd.DataFrame, int]:
    df = _base_rows()
    df.loc[205, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [657.20, 657.95, 657.34, 657.82, 657.70, 657.90, 657.60, -0.03]
    df.loc[206, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [657.05, 657.78, 657.18, 657.52, 657.58, 657.88, 657.50, -0.03]
    df.loc[207, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [656.92, 657.58, 657.02, 657.28, 657.46, 657.84, 657.42, -0.03]
    df.loc[208, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [656.80, 657.40, 656.90, 657.12, 657.34, 657.80, 657.35, -0.03]
    df.loc[209, ["low", "high", "close", "open", "ma10", "ma200", "vwap_day", "ma10_slope"]] = [656.72, 657.24, 656.78, 656.96, 657.22, 657.76, 657.26, -0.03]
    df.loc[210, ["open", "high", "low", "close", "ma10", "ma200", "vwap_day", "atr_14", "close_pos", "body", "range", "volume", "vol_ma20", "ma10_slope"]] = [
        656.96,
        657.02,
        656.40,
        656.48,
        657.10,
        657.70,
        657.05,
        0.29,
        0.13,
        0.48,
        0.62,
        1750.0,
        1200.0,
        -0.05,
    ]
    return df, 210


def test_spy_call_momentum_no_retest_signal_triggers_when_enabled() -> None:
    df, idx = _call_momentum_frame()
    strategy = MACrossStackContinuationV2DualExperimentalStrategy()
    strategy.enable_momentum_no_retest_dual = True
    row = df.iloc[idx]
    setup = strategy.describe_setup(df, idx, row)
    signal = strategy.detect_signal(df, idx, row, ReplayConfig())
    assert setup["call_trigger"] == "momentum_no_retest_call"
    assert signal is not None
    assert signal.side == "CALL"
    assert signal.signal_kind == "momentum_no_retest_dual"
    assert signal.regime == "STACK_BULL_MOMO_NO_RETEST_V2"


def test_spy_put_momentum_no_retest_signal_triggers_when_enabled() -> None:
    df, idx = _put_momentum_frame()
    strategy = MACrossStackContinuationV2DualExperimentalStrategy()
    strategy.enable_momentum_no_retest_dual = True
    row = df.iloc[idx]
    setup = strategy.describe_setup(df, idx, row)
    signal = strategy.detect_signal(df, idx, row, ReplayConfig())
    assert setup["put_trigger"] == "momentum_no_retest_put"
    assert signal is not None
    assert signal.side == "PUT"
    assert signal.signal_kind == "momentum_no_retest_dual"
    assert signal.regime == "STACK_BEAR_MOMO_NO_RETEST_V2"


def test_spy_cross_retest_keeps_priority_over_momentum_branch() -> None:
    df, idx = _put_cross_retest_frame()
    strategy = MACrossStackContinuationV2DualExperimentalStrategy()
    strategy.enable_momentum_no_retest_dual = True
    strategy.enable_vwap_as_soft_filter = False
    row = df.iloc[idx]
    signal = strategy.detect_signal(df, idx, row, ReplayConfig())
    assert signal is not None
    assert signal.side == "PUT"
    assert signal.signal_kind == "ma_cross_stack_cont_v2_dual"
    assert signal.regime == "STACK_BEAR_RETEST_V2"
