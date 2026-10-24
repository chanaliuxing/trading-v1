from __future__ import annotations

from dataclasses import dataclass

from strategies.stocks_kernel.types import HistoryBundle

from .history_loader import HistoryLoader


@dataclass(frozen=True)
class BackfillRequest:
    symbol: str
    resolution: str
    min_bars: int


@dataclass(frozen=True)
class BackfillResult:
    ready: bool
    reason: str
    history: HistoryBundle


class BackfillCoordinator:
    def __init__(self, loader: HistoryLoader) -> None:
        self._loader = loader

    def ensure_history(self, *, symbol: str, resolution: str, min_bars: int) -> BackfillResult:
        history = self._loader.load(symbol=symbol, resolution=resolution, limit=min_bars)
        ready = len(history.bars_1m) >= int(min_bars)
        return BackfillResult(
            ready=ready,
            reason="ready" if ready else "backfill_history_missing",
            history=history,
        )
