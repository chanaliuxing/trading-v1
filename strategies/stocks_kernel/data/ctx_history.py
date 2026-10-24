from __future__ import annotations

from typing import Any

from strategies.stocks_kernel.data.time_utils import coerce_epoch_seconds
from strategies.stocks_kernel.types import HistoryBar, HistoryBundle


async def load_history_from_ctx(*, ctx: Any, symbol: str, resolution: str, limit: int) -> HistoryBundle:
    rows = await ctx.history(symbol=symbol, resolution=resolution, limit=limit)
    bars = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts_epoch_s = coerce_epoch_seconds(row.get("ts_epoch_s") or row.get("ts"), default=0.0)
        bars.append(
            HistoryBar(
                ts_epoch_s=ts_epoch_s,
                open=float(row.get("open") or row.get("o") or 0.0),
                high=float(row.get("high") or row.get("h") or 0.0),
                low=float(row.get("low") or row.get("l") or 0.0),
                close=float(row.get("close") or row.get("c") or 0.0),
                volume=float(row.get("volume") or row.get("v") or 0.0),
            )
        )
    return HistoryBundle(symbol=str(symbol).strip().upper(), bars_1m=tuple(bars))
