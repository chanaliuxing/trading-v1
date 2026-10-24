from strategies.stocks_kernel.gates.pipeline import GatePipeline
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, HistoryBundle, MarketSnapshot


class _BoomGate:
    gate_id = "boom"

    def evaluate(self, *, symbol, feature_context, readiness):
        raise RuntimeError("boom")


def _ctx():
    return FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH"),
        history=HistoryBundle(symbol="QQQ"),
        values={},
        readiness={},
    )


def _ready():
    return DataReadinessReport(ready=True, reason="ready", freshness_ok=True, history_ready=True, snapshot_ready=True)


def test_gate_pipeline_fail_closed_blocks():
    report = GatePipeline([_BoomGate()], fail_mode="fail_closed").evaluate(symbol="QQQ", feature_context=_ctx(), readiness=_ready())
    assert report.allowed is False
    assert report.blocked_by == "boom"
    assert report.decisions[0].reason == "gate_failed_closed"


def test_gate_pipeline_fail_open_allows():
    report = GatePipeline([_BoomGate()], fail_mode="fail_open").evaluate(symbol="QQQ", feature_context=_ctx(), readiness=_ready())
    assert report.allowed is True
    assert report.blocked_by is None
    assert report.decisions[0].reason == "gate_failed_open"
