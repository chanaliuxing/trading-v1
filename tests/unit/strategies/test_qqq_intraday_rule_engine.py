from datetime import datetime

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.qqq_intraday_rules import (
    QQQIntradayRuleEngine,
    load_entry_recipe,
    load_option_entry_recipe,
    load_stock_entry_recipe,
)


def _runtime(now: datetime, *, spot: float, features: dict | None = None, payload: dict | None = None) -> PluginRuntimeState:
    f = dict(features or {})
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=spot,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=spot,
            high=spot + 0.5,
            low=spot - 0.5,
            close=spot,
            volume=float(f.get("volume", 1200.0)),
            vwap=float(f.get("vwap", spot)),
            atr=float(f.get("atr_eff", 1.0)),
        ),
        features=f,
        payload=dict(payload or {}),
    )


def test_recipe_validation_rejects_unknown_direction_module() -> None:
    try:
        load_entry_recipe(
            {
                "entry_recipe": {
                    "session_module": "baseline_rth_window",
                    "direction_module": "unknown",
                }
            }
        )
    except ValueError as exc:
        assert "invalid direction_module" in str(exc)
        return
    raise AssertionError("expected ValueError")


def test_engine_blocks_outside_window() -> None:
    engine = QQQIntradayRuleEngine()
    runtime = _runtime(
        datetime(2026, 4, 2, 15, 40, 0),
        spot=500.0,
        features={"v3m": 0.4, "vwap": 499.7, "atr_eff": 1.2, "atr_seed": 1.2},
    )
    extras = {"entry_model": "baseline"}
    result = engine.evaluate(runtime=runtime, extras=extras, recipe=load_entry_recipe(extras))
    assert result.translation.allowed is False
    assert result.translation.reason == "outside_session_window"


def test_engine_maps_long_to_call() -> None:
    engine = QQQIntradayRuleEngine()
    runtime = _runtime(
        datetime(2026, 4, 2, 10, 2, 0),
        spot=500.0,
        features={
            "v3m": 0.8,
            "vwap": 499.2,
            "atr_eff": 1.0,
            "atr_seed": 1.0,
            "qqew_decoupled": True,
        },
    )
    extras = {"entry_model": "baseline", "qqew_decoupled": True, "hv_min_vol_surge": 0.0}
    result = engine.evaluate(runtime=runtime, extras=extras, recipe=load_entry_recipe(extras))
    assert result.translation.allowed is True
    assert result.translation.stock_side == "LONG"
    assert result.translation.side == "CALL"


def test_engine_maps_short_to_put() -> None:
    engine = QQQIntradayRuleEngine()
    runtime = _runtime(
        datetime(2026, 4, 2, 10, 2, 0),
        spot=498.0,
        features={
            "v3m": -0.9,
            "vwap": 499.1,
            "atr_eff": 1.0,
            "atr_seed": 1.0,
            "qqew_decoupled": True,
        },
    )
    extras = {"entry_model": "baseline", "qqew_decoupled": True}
    result = engine.evaluate(runtime=runtime, extras=extras, recipe=load_entry_recipe(extras))
    assert result.translation.allowed is True
    assert result.translation.stock_side == "SHORT"
    assert result.translation.side == "PUT"


def test_shared_recipe_contract_uses_stock_translator() -> None:
    recipe = load_stock_entry_recipe({"entry_model": "v47_fast_drift"})
    assert recipe.direction_module == "fast_drift_direction"
    assert recipe.translator == "tqqq_sqqq_translation"


def test_shared_recipe_contract_uses_option_translator() -> None:
    recipe = load_option_entry_recipe({"entry_model": "v47_fast_drift"})
    assert recipe.direction_module == "fast_drift_direction"
    assert recipe.translator == "qqq_option_translation"


def test_high_vol_confirm_veto_blocks_weak_velocity_under_high_vol() -> None:
    engine = QQQIntradayRuleEngine()
    runtime = _runtime(
        datetime(2026, 4, 2, 10, 10, 0),
        spot=500.0,
        features={
            "v3m": 0.8,
            "vwap": 499.0,
            "atr_live": 3.0,
            "atr_eff": 1.0,
            "atr_seed": 1.0,
            "qqew_decoupled": True,
        },
    )
    extras = {"entry_model": "baseline", "qqew_decoupled": True}
    result = engine.evaluate(runtime=runtime, extras=extras, recipe=load_option_entry_recipe(extras))
    assert result.translation.allowed is False
    assert result.translation.reason in {"high_vol_no_volume", "high_vol_protection"}


def test_day_shape_overlay_blocks_exhausted_long() -> None:
    engine = QQQIntradayRuleEngine()
    runtime = _runtime(
        datetime(2026, 4, 2, 10, 10, 0),
        spot=500.0,
        features={
            "v3m": 1.1,
            "vwap": 499.0,
            "atr_eff": 1.0,
            "atr_seed": 1.0,
            "day_position_rank": 0.99,
            "qqew_decoupled": True,
        },
    )
    extras = {
        "entry_model": "baseline",
        "qqew_decoupled": True,
        "day_shape_overlay_enabled": True,
        "entry_recipe": {
            "session_module": "baseline_rth_window",
            "direction_module": "baseline_direction",
            "guards": [
                "amp_lock_guard",
                "stable_entry_guard",
                "restart_degrade_guard",
                "startup_anchor_guard",
                "open_orders_guard",
                "live_position_guard",
                "pending_decision_sync_guard",
                "ownership_guard",
                "qqq_freshness_guard",
                "qqew_freshness_guard",
                "continuity_180s_guard",
                "atr_ready_guard",
                "vwap_ready_guard",
                "volume_ready_guard",
            ],
            "overlays": ["day_shape_overlay"],
            "translator": "qqq_option_translation",
        },
    }
    result = engine.evaluate(runtime=runtime, extras=extras, recipe=load_option_entry_recipe(extras))
    assert result.translation.allowed is False
    assert result.translation.reason == "day_shape_long_exhaustion"


# ---------------------------------------------------------------------------
# ATR self-computation from bar_history (no atr_live in features)
# ---------------------------------------------------------------------------

def _make_bar_history(base_price: float, n: int, step: float = 0.05) -> list[dict]:
    """Build n synthetic 1m bars with realistic high/low."""
    from datetime import timedelta
    t0 = datetime(2026, 4, 7, 9, 30, 0)
    bars = []
    close = base_price
    for i in range(n):
        bars.append({
            "ts": t0 + timedelta(minutes=i),
            "open": close,
            "high": close + 0.25,
            "low": close - 0.20,
            "close": close,
            "volume": 1500.0,
            "vwap": close,
            "atr": 0.0,
        })
        close += step
    return bars


def test_atr_computed_from_bar_history_when_features_missing():
    """
    When features_1m has no atr_live/atr_seed (which is the real production case),
    build_entry_context should compute ATR from bar_history and return atr_eff > 0,
    so that atr_ready_guard passes.
    """
    from strategies.qqq_intraday_rules.runtime_context import build_entry_context

    now = datetime(2026, 4, 7, 10, 27, 0)
    history = _make_bar_history(480.0, 30)  # 30 bars — plenty for ATR(14)
    runtime = _runtime(
        now,
        spot=481.5,
        features={"vwap": 481.0},  # NO atr_live, atr_seed, atr_1m_qqq
        payload={"bar_history": history},
    )
    ctx = build_entry_context(runtime, {"qqew_decoupled": True})
    assert ctx.atr_live > 0, f"atr_live should be > 0, got {ctx.atr_live}"
    assert ctx.atr_seed > 0, f"atr_seed should be > 0, got {ctx.atr_seed}"
    assert ctx.atr_eff > 0, f"atr_eff should be > 0, got {ctx.atr_eff}"


def test_atr_ready_guard_passes_with_bar_history_only():
    """
    End-to-end: engine should not block on atr_ready when bar_history provides enough data,
    even when features dict has no atr_* keys at all.
    """
    engine = QQQIntradayRuleEngine()
    now = datetime(2026, 4, 7, 10, 27, 0)
    history = _make_bar_history(480.0, 30)
    runtime = _runtime(
        now,
        spot=481.5,
        features={"vwap": 481.0, "vol_ma10": 1500.0},
        payload={"bar_history": history},
    )
    extras = {
        "entry_model": "v47_fast_drift",
        "qqew_decoupled": True,
    }
    result = engine.evaluate(runtime=runtime, extras=extras, recipe=load_option_entry_recipe(extras))
    # Should NOT be blocked by atr_ready_guard specifically
    assert result.gate_reason != "data_not_ready" or result.gate_payload.get("atr_eff", -1) > 0, (
        f"atr_ready_guard should pass with bar_history; got gate_reason={result.gate_reason} "
        f"payload={result.gate_payload}"
    )


def test_atr_computed_insufficient_history():
    """With fewer than period+1 bars, ATR returns None → falls back to 0."""
    from strategies.qqq_intraday_rules.runtime_context import _compute_atr_from_rows, _MinuteRow

    t0 = datetime(2026, 4, 7, 9, 30, 0)
    rows = [_MinuteRow(ts=t0, close=480.0, volume=1000.0, vwap=480.0, high=480.5, low=479.5)]
    assert _compute_atr_from_rows(rows, period=14) is None
