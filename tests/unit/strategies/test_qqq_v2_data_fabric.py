from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from strategies.qqq_v2.engines.data_fabric_qqq import QQQDataFabric


ET = ZoneInfo("America/New_York")


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, event, **kwargs):
        self.errors.append((event, kwargs))


class _Fabric:
    def __init__(self):
        self.bars_1m = {"QQQ": [], "QQEW": []}
        self.bars_5m = {"TQQQ": [], "SQQQ": []}
        self.injected = []

    def inject_historical_1m(self, symbol, bars):
        self.injected.append((symbol, bars))


class _History:
    async def fetch_ibkr_1m_bars(self, symbol, start, end):
        return [], "ibkr_disconnected", 0

    async def fetch_local_db_1m_bars(self, symbol, start, end):
        return [], "ch_error", 0


@pytest.mark.asyncio
async def test_qqq_v2_warmup_marks_data_unavailable_on_clickhouse_error(tmp_path):
    logger = _Logger()
    state = {}
    fabric = _Fabric()
    qqq = QQQDataFabric(
        ctx=SimpleNamespace(log=logger),
        state=state,
        fabric=fabric,
        history=_History(),
        seed_path=str(tmp_path / "seed.json"),
    )

    await qqq.warmup_vwap_data(datetime(2026, 5, 1, 10, 0, tzinfo=ET))

    assert fabric.injected == []
    assert state["data_integrity_warning"] is True
    assert state["data_unavailable_gate"]["source"] == "ch_error"
    assert state["data_unavailable_gate"]["stage"] == "warmup_vwap_data"
    assert logger.errors[0][0] == "qqq_v2_data_unavailable_gate"
