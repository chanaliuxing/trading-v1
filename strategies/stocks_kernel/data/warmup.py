from __future__ import annotations

from dataclasses import dataclass

from .backfill_coordinator import BackfillCoordinator, BackfillResult


@dataclass(frozen=True)
class WarmupRequest:
    symbol: str
    min_history_bars: int
    resolution: str = "1m"


@dataclass(frozen=True)
class WarmupResult:
    ready: bool
    reason: str
    bars_loaded: int


class WarmupCoordinator:
    def __init__(self, backfill: BackfillCoordinator) -> None:
        self._backfill = backfill

    def warmup(self, *, symbol: str, min_history_bars: int, resolution: str = "1m") -> WarmupResult:
        result: BackfillResult = self._backfill.ensure_history(
            symbol=symbol,
            resolution=resolution,
            min_bars=min_history_bars,
        )
        return WarmupResult(
            ready=result.ready,
            reason="ready" if result.ready else "warmup_history_missing",
            bars_loaded=len(result.history.bars_1m),
        )
