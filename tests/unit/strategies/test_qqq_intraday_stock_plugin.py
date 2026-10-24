from __future__ import annotations

from types import SimpleNamespace

import pytest

from strategies.stocks_kernel.types import HistoryBar
from strategies.qqq_stock_signal_artifact import get_latest_stock_entry_signal
from strategies.stocks_kernel.plugins.qqq_intraday import QQQIntradayStockPlugin


@pytest.mark.asyncio
async def test_qqq_intraday_stock_plugin_publishes_option_signal_from_entry_intent() -> None:
    plugin = QQQIntradayStockPlugin()
    strategy = SimpleNamespace(strategy_id="qqq_intraday_plugin_001")
    envelope = SimpleNamespace(
        decision_id="decision-1",
        extras={"intent_action": "entry", "trigger_id": "trigger-1", "trace_id": "trace-1"},
    )

    await plugin.on_features(
        strategy=strategy,
        symbol="QQQ",
        features={
            "ts_epoch_s": 1778597160.0,
            "v3m_signed": -0.6,
            "atr_eff": 0.3,
            "atr_live": 0.3,
            "atr_seed": 0.25,
            "atr_cap": 0.4,
            "vol_surge_ratio": 1.2,
            "distance_from_vwap_atr": 2.1,
            "data_age_qqq_sec": 1.0,
            "continuity_180s_ok": True,
            "qqq_now": 704.5,
            "vwap_now": 706.0,
        },
    )

    returned = await plugin.on_intent(strategy=strategy, envelope=envelope, symbol="SQQQ", side="SHORT")

    assert returned is envelope
    assert envelope.extras["stock_plugin_component"] == "qqq_intraday_stock_plugin"
    assert envelope.extras["stock_signal_plane_publish"] == "ok"
    artifact = get_latest_stock_entry_signal(underlying="QQQ", source_strategy_id="qqq_intraday_plugin_001")
    assert artifact is not None
    assert artifact.trigger_id == "trigger-1"
    assert artifact.instrument == "SQQQ"
    assert artifact.direction == "SHORT"


def test_qqq_intraday_stock_plugin_manifest_matches_live_contract() -> None:
    manifest = QQQIntradayStockPlugin().production_manifest()

    assert manifest is not None
    assert manifest.strategy_key == "qqq_intraday_stock_plugin"
    assert manifest.symbols == ("QQQ", "TQQQ", "SQQQ")
    assert manifest.policy_id == "qqq_intraday_policy_v1"
    assert manifest.recovery_policy_id == "qqq_kernel_close_only"


@pytest.mark.asyncio
async def test_qqq_intraday_stock_plugin_injects_legacy_seed_state_and_v3m() -> None:
    plugin = QQQIntradayStockPlugin()
    strategy = SimpleNamespace(
        strategy_id="qqq_intraday_plugin_001",
        config=SimpleNamespace(policy_params={"first_entry_seed_min_samples": 2}),
    )
    start = 1778580000.0

    updated = await plugin.on_features(
        strategy=strategy,
        symbol="QQQ",
        features={
            "ts_epoch_s": start + 205.0,
            "last_price": 101.0,
            "bars_5s": [
                HistoryBar(ts_epoch_s=start, open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0),
                HistoryBar(ts_epoch_s=start + 180.0, open=100.5, high=100.5, low=100.5, close=100.5, volume=1.0),
                HistoryBar(ts_epoch_s=start + 185.0, open=100.7, high=100.7, low=100.7, close=100.7, volume=1.0),
            ],
        },
    )

    assert updated is not None
    assert updated["seed_n"] >= 2
    assert updated["seed_ready"] is True
    assert updated["v3m_units"] == "ratio"
    assert updated["v3m_signed"] == pytest.approx((101.0 - 100.0) / 100.0)
