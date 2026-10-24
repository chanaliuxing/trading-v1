from __future__ import annotations

from dataclasses import replace
from typing import Any

from strategies.stocks_kernel.data.time_utils import coerce_epoch_seconds
from strategies.stocks_kernel.types import MarketSnapshot


def merge_snapshot(base: MarketSnapshot | None, **updates: Any) -> MarketSnapshot:
    if base is None:
        symbol = str(updates.pop("symbol", "") or "").strip().upper()
        ts_epoch_s = coerce_epoch_seconds(updates.pop("ts_epoch_s", 0.0), default=0.0)
        extras = dict(updates.pop("extras", {}) or {})
        if not symbol or ts_epoch_s <= 0:
            raise ValueError("symbol and ts_epoch_s are required")
        return MarketSnapshot(symbol=symbol, ts_epoch_s=ts_epoch_s, extras=extras, **updates)
    sanitized = dict(updates)
    if "symbol" in sanitized:
        sanitized["symbol"] = str(sanitized["symbol"] or "").strip().upper()
    if "extras" in sanitized:
        sanitized["extras"] = dict(sanitized["extras"] or {})
    if "ts_epoch_s" in sanitized:
        sanitized["ts_epoch_s"] = coerce_epoch_seconds(sanitized["ts_epoch_s"], default=base.ts_epoch_s)
    return replace(base, **sanitized)
