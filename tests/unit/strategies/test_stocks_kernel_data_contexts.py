from strategies.stocks_kernel.data.contexts import build_data_context_bundle
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, HistoryBundle, MarketSnapshot


def test_build_data_context_bundle_supports_signal_exec_confirm_hedge_maps():
    signal_snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0)
    signal_history = HistoryBundle(symbol="QQQ")
    signal_readiness = DataReadinessReport(
        ready=True,
        reason="ready",
        freshness_ok=True,
        history_ready=True,
        snapshot_ready=True,
    )
    signal_ctx = FeatureContext(symbol="QQQ", snapshot=signal_snapshot, history=signal_history, values={"v3m": 0.1}, scope="signal")

    bundle = build_data_context_bundle(
        signal_symbol="QQQ",
        signal_snapshot=signal_snapshot,
        signal_history=signal_history,
        signal_readiness=signal_readiness,
        signal_feature_context=signal_ctx,
        exec_contexts={"TQQQ": {"snapshot": MarketSnapshot(symbol="TQQQ", ts_epoch_s=100.0, last_price=50.0)}},
        confirm_contexts={"VXN": {"snapshot": MarketSnapshot(symbol="VXN", ts_epoch_s=100.0, last_price=18.0)}},
        hedge_contexts={"SQQQ": {"snapshot": MarketSnapshot(symbol="SQQQ", ts_epoch_s=100.0, last_price=12.0)}},
    )

    assert bundle.signal.symbol == "QQQ"
    assert bundle.execution["TQQQ"].scope == "execution"
    assert bundle.confirm["VXN"].scope == "confirm"
    assert bundle.hedge["SQQQ"].scope == "hedge"
    assert bundle.symbols_for_scope("execution") == ("TQQQ",)
    assert bundle.context_for(scope="confirm", symbol="VXN").symbol == "VXN"
    assert bundle.summary()["signal"]["symbols"] == ("QQQ",)
