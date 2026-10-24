from datetime import datetime

from strategies.stocks_kernel.features import FeaturePlugin
from strategies.stocks_kernel.data.market_snapshot import merge_snapshot
from strategies.stocks_kernel.data.readiness import evaluate_data_readiness
from strategies.stocks_kernel.data.providers import FeatureRowSnapshotProvider
from strategies.stocks_kernel.data.time_utils import (
    coerce_feature_timestamp_seconds,
    resolve_session_label,
    session_label_from_epoch_seconds,
)
from strategies.stocks_kernel.features.registry import FeatureRegistry, default_feature_registry
from strategies.stocks_kernel.data.contexts import build_data_context_bundle
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, HistoryBar, HistoryBundle, MarketSnapshot


def _history(symbol: str = "QQQ") -> HistoryBundle:
    return HistoryBundle(
        symbol=symbol,
        bars_1m=(
            HistoryBar(ts_epoch_s=1, open=100, high=101, low=99, close=100, volume=10),
            HistoryBar(ts_epoch_s=2, open=100, high=102, low=99, close=101, volume=20),
            HistoryBar(ts_epoch_s=3, open=101, high=103, low=100, close=102, volume=30),
            HistoryBar(ts_epoch_s=4, open=102, high=104, low=101, close=103, volume=40),
            HistoryBar(ts_epoch_s=5, open=103, high=105, low=102, close=104, volume=50),
            HistoryBar(ts_epoch_s=6, open=104, high=106, low=103, close=105, volume=60),
            HistoryBar(ts_epoch_s=7, open=105, high=107, low=104, close=106, volume=70),
            HistoryBar(ts_epoch_s=8, open=106, high=108, low=105, close=107, volume=80),
            HistoryBar(ts_epoch_s=9, open=107, high=109, low=106, close=108, volume=90),
            HistoryBar(ts_epoch_s=10, open=108, high=110, low=107, close=109, volume=100),
            HistoryBar(ts_epoch_s=11, open=109, high=111, low=108, close=110, volume=110),
            HistoryBar(ts_epoch_s=12, open=110, high=112, low=109, close=111, volume=120),
            HistoryBar(ts_epoch_s=13, open=111, high=113, low=110, close=112, volume=130),
            HistoryBar(ts_epoch_s=14, open=112, high=114, low=111, close=113, volume=140),
            HistoryBar(ts_epoch_s=15, open=113, high=115, low=112, close=114, volume=150),
        ),
    )


def test_merge_snapshot_and_readiness_ready_path():
    snapshot = merge_snapshot(None, symbol="qqq", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    history = _history()

    report = evaluate_data_readiness(snapshot=snapshot, history=history, now_epoch_s=103.0, max_staleness_sec=8.0, min_history_bars=5)

    assert snapshot.symbol == "QQQ"
    assert report.ready is True
    assert report.reason == "ready"


def test_default_feature_registry_builds_context():
    registry = default_feature_registry()
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, vwap=108.0)
    history = _history()

    ctx = registry.build_context(
        symbol="QQQ",
        snapshot=snapshot,
        history=history,
        feature_ids=["vwap", "atr_eff", "v3m", "pos_day", "vol_surge", "distance_from_vwap_atr"],
    )

    assert ctx.values["vwap"] == 108.0
    assert ctx.values["atr_eff"] is not None
    assert ctx.values["v3m"] is not None
    assert ctx.values["pos_day"] is not None
    assert ctx.values["vol_surge"] is not None
    assert ctx.values["distance_from_vwap_atr"] is not None


def test_merge_snapshot_accepts_datetime_timestamp():
    snapshot = merge_snapshot(None, symbol="qqq", ts_epoch_s=datetime(2026, 3, 31, 9, 30, 0), last_price=500.0, session="RTH")

    assert snapshot.symbol == "QQQ"
    assert snapshot.ts_epoch_s > 0


def test_time_utils_normalize_session_labels_and_phase():
    assert resolve_session_label("premarket") == "PRE"
    assert resolve_session_label("afterhours") == "POST"
    assert session_label_from_epoch_seconds(datetime(2026, 3, 31, 12, 10, 0).timestamp()) == "RTH"


def test_time_utils_parse_feature_timestamp_alias_units():
    assert coerce_feature_timestamp_seconds(datetime(2026, 3, 31, 12, 16, 0)) > 0
    assert coerce_feature_timestamp_seconds(1774973759956986) == 1774973759.956986
    assert coerce_feature_timestamp_seconds(1774973759956) == 1774973759.956


def test_feature_registry_supports_dependency_resolution_and_params():
    registry = FeatureRegistry()
    calls = {"base": 0, "derived": 0}

    def calc_base(snapshot, history, params):
        calls["base"] += 1
        return registry_result(5.0)

    def calc_derived(snapshot, history, params):
        calls["derived"] += 1
        dependencies = snapshot.extras["_feature_dependency_values"]
        return registry_result(float(dependencies["base"]) * float(params["multiplier"]))

    registry.register(FeaturePlugin(feature_id="base", calculator=calc_base))
    registry.register(
        FeaturePlugin(
            feature_id="derived",
            calculator=calc_derived,
            dependencies=("base",),
            default_params={"multiplier": 2},
        )
    )
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0)
    history = _history()

    ctx = registry.build_context(
        symbol="QQQ",
        snapshot=snapshot,
        history=history,
        feature_ids=["derived", "base"],
        feature_params={"derived": {"multiplier": 3}},
    )

    assert ctx.values["base"] == 5.0
    assert ctx.values["derived"] == 15.0
    assert calls == {"base": 1, "derived": 1}


def registry_result(value):
    from strategies.stocks_kernel.types import FeatureResult

    return FeatureResult(value=value, ready=True, reason="ready")


def test_feature_row_snapshot_provider_passes_gate_extras_and_derives_planned_notional():
    snapshot = FeatureRowSnapshotProvider.build_snapshot(
        symbol="smh",
        features={
            "ts_epoch_s": 100.0,
            "last_price": 123.0,
            "planned_qty": 2,
            "trigger_id": "t-1",
            "same_strategy_pending_orders": 1,
            "foreign_position_qty": 0.5,
            "current_notional": 1000.0,
            "seed_n": 800,
            "seed_ready": True,
            "anti_chase_enabled": True,
            "anti_chase_rank15": 0.97,
            "vol_ratio_override": 0.82,
        },
    )

    assert snapshot.symbol == "SMH"
    assert snapshot.extras["planned_qty"] == 2
    assert snapshot.extras["trigger_id"] == "t-1"
    assert snapshot.extras["same_strategy_pending_orders"] == 1
    assert snapshot.extras["foreign_position_qty"] == 0.5
    assert snapshot.extras["current_notional"] == 1000.0
    assert snapshot.extras["seed_n"] == 800
    assert snapshot.extras["seed_ready"] is True
    assert snapshot.extras["anti_chase_enabled"] is True
    assert snapshot.extras["anti_chase_rank15"] == 0.97
    assert snapshot.extras["vol_ratio_override"] == 0.82
    assert snapshot.extras["planned_notional"] == 246.0


def test_feature_registry_builds_scoped_feature_overlay_from_data_context_bundle():
    registry = default_feature_registry()
    signal_snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH", vwap=500.0)
    signal_history = _history("QQQ")
    signal_ctx = FeatureContext(symbol="QQQ", snapshot=signal_snapshot, history=signal_history, values={}, readiness={})
    ready = DataReadinessReport(ready=True, reason="ready", freshness_ok=True, history_ready=True, snapshot_ready=True)
    exec_snapshot = MarketSnapshot(symbol="TQQQ", ts_epoch_s=100.0, last_price=50.0, session="RTH", vwap=49.5)
    exec_history = _history("TQQQ")

    bundle = build_data_context_bundle(
        signal_symbol="QQQ",
        signal_snapshot=signal_snapshot,
        signal_history=signal_history,
        signal_readiness=ready,
        signal_feature_context=signal_ctx,
        exec_contexts={
            "TQQQ": {
                "snapshot": exec_snapshot,
                "history": exec_history,
                "readiness": ready,
            }
        },
    )

    values, readiness = registry.build_scoped_feature_overlay(
        data_context_bundle=bundle,
        scope_feature_ids={"execution": ["vwap", "v3m"]},
    )

    assert "execution.TQQQ.vwap" in values
    assert "execution.vwap" in values
    assert readiness["execution.vwap"] == "ready"
