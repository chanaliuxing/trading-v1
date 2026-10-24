from __future__ import annotations

from dataclasses import dataclass

from .backfill_coordinator import BackfillCoordinator


@dataclass(frozen=True)
class OffHoursBackfillPlan:
    symbol: str
    session_mode: str
    resolution: str
    min_history_bars: int


@dataclass(frozen=True)
class OffHoursBackfillResult:
    ready: bool
    reason: str
    loaded_bars: int


class OffHoursBackfillCoordinator:
    def __init__(self, backfill: BackfillCoordinator) -> None:
        self._backfill = backfill

    def run(self, *, symbol: str, min_history_bars: int, resolution: str = "1m") -> OffHoursBackfillResult:
        result = self._backfill.ensure_history(symbol=symbol, resolution=resolution, min_bars=min_history_bars)
        return OffHoursBackfillResult(
            ready=result.ready,
            reason="ready" if result.ready else "offhours_backfill_missing",
            loaded_bars=len(result.history.bars_1m),
        )
