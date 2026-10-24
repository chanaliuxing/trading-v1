from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.market import options_quote_memory_cache as cache

ET = ZoneInfo("America/New_York")


def _mk_row(ts: datetime, underlying: str, strike: float, right: str, dte: int = 1) -> tuple:
    return (
        ts,  # 0 ts
        underlying,  # 1 underlying
        ts.date(),  # 2 expiry
        strike,  # 3 strike
        right,  # 4 right
        dte,  # 5 dte
        1.0,  # 6 bid
        1.1,  # 7 ask
        1.05,  # 8 mid
        1.05,  # 9 last
        0,  # 10 volume
        0,  # 11 open_interest
        0.2,  # 12 iv
        0.5,  # 13 delta
        0.01,  # 14 gamma
        -0.01,  # 15 theta
        0.02,  # 16 vega
        10.0,  # 17 spread_bps
        100,  # 18 contracts_mult
        "ibkr",  # 19 source
        ts + timedelta(seconds=5),  # 20 created_at
    )


def _reset_store() -> None:
    with cache._LOCK:
        cache._STORE.clear()


def test_cache_prunes_old_snapshots_by_age() -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    old_ts = now - timedelta(minutes=10)
    fresh_ts = now

    cache.update_from_ingest_rows([_mk_row(old_ts, "SPY", 650.0, "C")])
    cache.update_from_ingest_rows([_mk_row(fresh_ts, "SPY", 655.0, "C")])

    rows = cache.query_rows(
        underlying="SPY",
        right="C",
        dte_min=1,
        dte_max=2,
        ts_lb=now - timedelta(minutes=2),
        ts_now=now,
    )
    assert rows
    assert all(r["ts"] >= now - timedelta(minutes=2) for r in rows)


def test_cache_is_bounded_by_snapshot_count() -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)

    # Push more snapshots than max; deque should keep only the latest bounded size.
    for i in range(cache._MAX_SNAPSHOTS_PER_UNDERLYING + 4):
        ts = now - timedelta(minutes=i)
        cache.update_from_ingest_rows([_mk_row(ts, "QQQ", 500.0 + i, "P")])

    with cache._LOCK:
        qqq_buf = list(cache._STORE.get("QQQ", []))
    assert len(qqq_buf) <= cache._MAX_SNAPSHOTS_PER_UNDERLYING
