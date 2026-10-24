from strategies.stocks_kernel.data.backfill_coordinator import BackfillCoordinator
from strategies.stocks_kernel.data.history_loader import HistoryLoader
from strategies.stocks_kernel.types import HistoryBar, HistoryBundle


def test_backfill_coordinator_reports_ready_when_history_meets_requirement():
    loader = HistoryLoader(
        lambda req: HistoryBundle(
            symbol=req.symbol,
            bars_1m=tuple(
                HistoryBar(ts_epoch_s=float(i), open=1, high=2, low=0.5, close=1.5, volume=10)
                for i in range(req.limit)
            ),
        )
    )
    result = BackfillCoordinator(loader).ensure_history(symbol="QQQ", resolution="1m", min_bars=5)

    assert result.ready is True
    assert result.reason == "ready"
    assert len(result.history.bars_1m) == 5
