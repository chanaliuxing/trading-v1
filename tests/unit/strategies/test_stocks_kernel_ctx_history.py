from datetime import datetime
from types import SimpleNamespace

import pytest

from strategies.stocks_kernel.data.ctx_history import load_history_from_ctx


@pytest.mark.asyncio
async def test_load_history_from_ctx_converts_rows_to_history_bundle():
    class _Ctx:
        async def history(self, symbol, resolution, limit):
            return [
                {"ts_epoch_s": 1.0, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
                {"ts_epoch_s": 2.0, "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 20},
            ]

    bundle = await load_history_from_ctx(ctx=_Ctx(), symbol="qqq", resolution="1m", limit=2)

    assert bundle.symbol == "QQQ"
    assert len(bundle.bars_1m) == 2
    assert bundle.bars_1m[0].close == 100.5


@pytest.mark.asyncio
async def test_load_history_from_ctx_accepts_datetime_timestamps():
    class _Ctx:
        async def history(self, symbol, resolution, limit):
            return [
                {"ts": datetime(2026, 3, 31, 9, 30, 0), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
            ]

    bundle = await load_history_from_ctx(ctx=_Ctx(), symbol="qqq", resolution="1m", limit=1)

    assert bundle.symbol == "QQQ"
    assert len(bundle.bars_1m) == 1
    assert bundle.bars_1m[0].ts_epoch_s > 0
    assert bundle.bars_1m[0].close == 100.5
