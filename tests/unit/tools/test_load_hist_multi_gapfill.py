import types
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Provide a lightweight stub for asyncpg so importing libs.db.postgres does not fail
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = types.SimpleNamespace()

from tool.load_hist_multi import HistoricalDataLoader


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return datetime(2026, 4, 1, 12, 0, 0)
        return datetime(2026, 4, 1, 12, 0, 0, tzinfo=tz)


class DummyIB:
    def __init__(self) -> None:
        self.calls = []

    async def reqHistoricalDataAsync(self, **kwargs):
        self.calls.append(kwargs)
        # Return a non-empty list so loader treats chunk as having data
        return [object()]


class DummyClickHouse:
    def __init__(self, counts_by_date):
        # counts_by_date: Dict[date, int]
        self.counts_by_date = counts_by_date or {}
        # Minimal client stub used by _insert_bars_to_clickhouse
        self.client = types.SimpleNamespace(insert_df=lambda *args, **kwargs: None)

    def get_last_bar_timestamp(self, symbol, table=None):
        # Force backfill mode to use requested window only
        return None

    def query(self, sql, parameters=None):
        # Emulate the gap-scan query:
        # SELECT toDate(ts_start) AS d, count() AS c ... WHERE ts_start in [s, e)
        from datetime import datetime as _dt, timedelta as _td

        params = parameters or {}
        s = params.get("s") or params.get("start_ts")
        e = params.get("e") or params.get("end_ts")
        if not s or not e:
            return []
        sd = _dt.fromisoformat(str(s)).date()
        ed = _dt.fromisoformat(str(e)).date() - _td(days=1)

        rows = []
        for d, c in self.counts_by_date.items():
            if sd <= d <= ed:
                rows.append({"d": d, "c": c})
        return rows


@pytest.mark.asyncio
async def test_skip_covered_days_skips_all_when_fully_covered(monkeypatch):
    """When coverage is already full for each day, no IB requests are made."""
    monkeypatch.setattr("tool.load_hist_multi.datetime", _FixedDateTime)
    loader = HistoricalDataLoader(
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=999,
    )

    # Build coverage map: last 3 days all fully covered
    today = _FixedDateTime.now(loader.ib_timezone).date()
    day_0 = today
    day_1 = today - timedelta(days=1)
    day_2 = today - timedelta(days=2)

    counts = {}
    for d in (day_2, day_1, day_0):
        expected = loader._expected_bars_for_day("1 min", d)
        counts[d] = expected

    loader.ch = DummyClickHouse(counts)
    loader.ib = DummyIB()

    async def fake_insert(self, symbol, table, bar_secs, bars, is_backfill, meta):
        return len(bars)

    loader._insert_bars_to_clickhouse = fake_insert.__get__(loader, HistoricalDataLoader)

    inserted = await loader.load_data_for_symbol(
        symbol="TSLA",
        days=3,
        bar_size="1m",
        fill_mode="backfill",
        max_days_per_req_override=1,
        skip_covered_days=True,
        coverage_threshold=0.98,
    )

    assert inserted == 0
    assert len(loader.ib.calls) == 0


@pytest.mark.asyncio
async def test_skip_covered_days_fetches_only_incomplete_days(monkeypatch):
    """Gap-aware backfill should only fetch days with insufficient coverage."""
    monkeypatch.setattr("tool.load_hist_multi.datetime", _FixedDateTime)
    loader = HistoricalDataLoader(
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=1000,
    )

    today = _FixedDateTime.now(loader.ib_timezone).date()
    day_0 = today
    day_1 = today - timedelta(days=1)
    day_2 = today - timedelta(days=2)

    counts = {}
    # Day 2 and Day 0 fully covered
    for d in (day_2, day_0):
        expected = loader._expected_bars_for_day("1 min", d)
        counts[d] = expected
    # Day 1 only half covered -> should be treated as incomplete
    expected_mid = loader._expected_bars_for_day("1 min", day_1)
    counts[day_1] = max(1, expected_mid // 2)

    loader.ch = DummyClickHouse(counts)
    loader.ib = DummyIB()

    async def fake_insert(self, symbol, table, bar_secs, bars, is_backfill, meta):
        return len(bars)

    loader._insert_bars_to_clickhouse = fake_insert.__get__(loader, HistoricalDataLoader)

    inserted = await loader.load_data_for_symbol(
        symbol="TSLA",
        days=3,
        bar_size="1m",
        fill_mode="backfill",
        max_days_per_req_override=1,
        skip_covered_days=True,
        coverage_threshold=0.98,
    )

    # Only one day (middle) should be fetched
    assert len(loader.ib.calls) == 1
    call = loader.ib.calls[0]
    assert call.get("durationStr") == "1 D"
    assert inserted > 0
