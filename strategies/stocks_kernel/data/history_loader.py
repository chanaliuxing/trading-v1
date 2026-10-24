from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from strategies.stocks_kernel.types import HistoryBundle


@dataclass(frozen=True)
class HistoryLoadRequest:
    symbol: str
    resolution: str
    limit: int


class HistoryLoader:
    def __init__(self, fetcher: Callable[[HistoryLoadRequest], HistoryBundle]) -> None:
        self._fetcher = fetcher

    def load(self, *, symbol: str, resolution: str, limit: int) -> HistoryBundle:
        req = HistoryLoadRequest(
            symbol=str(symbol or "").strip().upper(),
            resolution=str(resolution or "").strip(),
            limit=int(limit),
        )
        if not req.symbol or not req.resolution or req.limit <= 0:
            raise ValueError("symbol/resolution/limit are required")
        return self._fetcher(req)
