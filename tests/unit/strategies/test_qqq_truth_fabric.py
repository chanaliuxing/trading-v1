from datetime import datetime

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.qqq_intraday_rules.runtime_context import build_entry_context
from strategies.qqq_intraday_rules.truth_fabric import resolve_qqq_truth_fabric


def _runtime(now: datetime) -> PluginRuntimeState:
    features = {
        "v3m_signed": 0.8,
        "vwap": 500.0,
        "atr_live": 1.1,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.3,
        "distance_from_vwap_atr": 0.2,
        "qqew_now": 99.1,
        "qqew_vwap_now": 98.9,
        "qqew_decoupled": True,
        "data_age_qqq_sec": 1.2,
        "data_age_qqew_sec": 1.6,
        "continuity_180s_ok": True,
        "session_open_price": 498.0,
        "day_position_rank": 0.55,
        "close_by_minute": {now.replace(second=0, microsecond=0): 500.2},
    }
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=500.2,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=500.0,
            high=500.3,
            low=499.8,
            close=500.2,
            volume=1800.0,
            vwap=500.0,
            atr=1.0,
            payload=features,
        ),
        features=features,
        active_position=None,
        active_positions=[],
        payload={},
    )


def test_truth_fabric_contains_required_fields() -> None:
    now = datetime(2026, 4, 9, 10, 20, 0)
    truth = resolve_qqq_truth_fabric(_runtime(now), {"entry_model": "v47_fast_drift"})
    assert truth.symbol == "QQQ"
    assert truth.spot > 0
    assert truth.vwap > 0
    assert truth.v3m_signed != 0
    assert truth.atr_eff > 0
    assert truth.vol_surge_ratio >= 0
    assert isinstance(truth.close_by_minute, dict)
    payload = truth.as_signal_payload()
    assert payload["spot"] > 0
    assert payload["continuity_180s_ok"] is True


def test_build_entry_context_uses_truth_fabric_accessor() -> None:
    now = datetime(2026, 4, 9, 10, 25, 0)
    ctx = build_entry_context(_runtime(now), {"entry_model": "v47_fast_drift"})
    assert ctx.symbol == "QQQ"
    assert ctx.qqq_now > 0
    assert ctx.vwap_now > 0
    assert ctx.atr_eff > 0
    assert ctx.continuity_180s_ok is True


def test_truth_fabric_supports_generic_spy_rsp_aliases() -> None:
    now = datetime(2026, 4, 9, 10, 20, 0)
    runtime = _runtime(now)
    runtime = runtime.__class__(
        strategy_id=runtime.strategy_id,
        kernel_mode=runtime.kernel_mode,
        symbol="SPY",
        now=runtime.now,
        wall_now=runtime.wall_now,
        spot=501.2,
        bar=KernelBarContext(
            ts=now,
            symbol="SPY",
            open=501.0,
            high=501.4,
            low=500.8,
            close=501.2,
            volume=1800.0,
            vwap=500.9,
            atr=1.0,
            payload=runtime.features,
        ),
        features={
            **dict(runtime.features),
            "rsp_now": 201.5,
            "rsp_vwap_now": 201.1,
            "data_age_spy_sec": 1.1,
            "data_age_rsp_sec": 1.4,
            "atr_1m_spy": 1.05,
        },
        active_position=None,
        active_positions=[],
        payload={},
    )
    truth = resolve_qqq_truth_fabric(
        runtime,
        {
            "entry_model": "baseline",
            "primary_symbol": "SPY",
            "confirm_symbol": "RSP",
            "confirm_feature_prefix": "rsp",
            "confirm_decoupled": False,
        },
    )
    assert truth.symbol == "SPY"
    assert truth.qqew_now == 201.5
    assert truth.qqew_vwap_now == 201.1
    assert truth.data_age_qqq_sec == 1.1
    assert truth.data_age_qqew_sec == 1.4
