from strategies.stocks_kernel.data.backfill_coordinator import BackfillCoordinator
from strategies.stocks_kernel.data.history_loader import HistoryLoader
from strategies.stocks_kernel.data.offhours import OffHoursBackfillCoordinator
from strategies.stocks_kernel.data.warmup import WarmupCoordinator
from strategies.stocks_kernel.types import HistoryBar, HistoryBundle


def _loader(count: int):
    return HistoryLoader(
        lambda req: HistoryBundle(
            symbol=req.symbol,
            bars_1m=tuple(
                HistoryBar(ts_epoch_s=float(i), open=1, high=2, low=0.5, close=1.5, volume=10)
                for i in range(count)
            ),
        )
    )


def test_warmup_and_offhours_coordinators_report_ready():
    backfill = BackfillCoordinator(_loader(20))
    warmup = WarmupCoordinator(backfill)
    offhours = OffHoursBackfillCoordinator(backfill)

    warm = warmup.warmup(symbol="QQQ", min_history_bars=15)
    off = offhours.run(symbol="QQQ", min_history_bars=15)

    assert warm.ready is True
    assert warm.bars_loaded == 20
    assert off.ready is True
    assert off.loaded_bars == 20
