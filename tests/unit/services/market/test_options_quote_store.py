from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.market import options_quote_memory_cache as cache
from services.market.options_quote_store import (
    HOTPATH_QUOTE_REQUIRED_KEYS,
    get_quote_for_opt_symbol,
    latest_quote_age_sec,
    latest_quote_status,
    query_option_rows,
    quote_minute_coverage,
    quote_store_hotpath_contract,
    validate_hotpath_quote_payload,
)
from libs.options.internal_symbol import format_internal_opt_symbol

ET = ZoneInfo("America/New_York")


def _mk_row(ts: datetime, underlying: str, strike: float, right: str, dte: int = 0) -> tuple:
    expiry = ts.date()
    return (
        ts,
        underlying,
        expiry,
        strike,
        right,
        dte,
        1.0,
        1.2,
        1.1,
        1.1,
        0,
        0,
        0.2,
        0.5 if right == "C" else -0.5,
        0.01,
        -0.01,
        0.02,
        10.0,
        100,
        "ibkr",
        ts + timedelta(seconds=5),
    )


def _reset_store() -> None:
    with cache._LOCK:
        cache._STORE.clear()


def test_store_prefers_memory_for_latest_age_when_db_empty(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())

    age_sec = latest_quote_age_sec(
        underlying="QQQ",
        now=now,
        day_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        day_end=(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        dte_min=0,
        dte_max=0,
        expiry=now.strftime("%Y-%m-%d"),
    )
    assert age_sec == 55.0


def test_store_prefers_fresher_clickhouse_age_when_newer_than_memory(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=2), "QQQ", 500.0, "C")])

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return [{"row_count": 1, "last_created_at": now - timedelta(seconds=20), "last_ts": now - timedelta(seconds=20)}]

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    age_sec = latest_quote_age_sec(
        underlying="QQQ",
        now=now,
        day_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        day_end=(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        dte_min=0,
        dte_max=0,
        expiry=now.strftime("%Y-%m-%d"),
    )
    assert age_sec == 20.0


def test_latest_quote_status_exposes_selected_source(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=2), "QQQ", 500.0, "C")])

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return [{"row_count": 1, "last_created_at": now - timedelta(seconds=15), "last_ts": now - timedelta(seconds=15)}]

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    status = latest_quote_status(
        underlying="QQQ",
        now=now,
        day_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        day_end=(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        dte_min=0,
        dte_max=0,
        expiry=now.strftime("%Y-%m-%d"),
    )
    assert status is not None
    assert status["source"] == "clickhouse"
    assert status["age_sec"] == 15.0


def test_latest_quote_status_ignores_empty_clickhouse_aggregate(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return [{"row_count": 0, "last_created_at": datetime(1970, 1, 1, tzinfo=ET), "last_ts": datetime(1970, 1, 1, tzinfo=ET)}]

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    status = latest_quote_status(
        underlying="QQQ",
        now=now,
        day_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        day_end=(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        dte_min=0,
        dte_max=0,
        expiry=now.strftime("%Y-%m-%d"),
    )

    assert status is None


def test_store_coverage_uses_memory_rows(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([
        _mk_row(now - timedelta(minutes=2), "QQQ", 500.0, "C"),
        _mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "P"),
    ])

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    counts = quote_minute_coverage(
        underlying="QQQ",
        window_start=now - timedelta(minutes=3),
        window_end=now,
        dte_min=0,
        dte_max=0,
        expiry=now.strftime("%Y-%m-%d"),
    )
    assert counts == {"covered_minutes": 2, "nbbo_minutes": 2}


def test_store_reads_contract_quote_from_memory_before_db(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])
    opt_symbol = format_internal_opt_symbol(
        underlying="QQQ",
        expiry_ymd=now.strftime("%Y%m%d"),
        strike=500.0,
        right="C",
    )

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    quote = get_quote_for_opt_symbol(
        now=now,
        opt_symbol=opt_symbol,
        lookback_min=2,
        max_age_sec=90.0,
        dte_min=0,
        dte_max=0,
    )
    assert quote is not None
    assert quote["mid"] == 1.1


def test_query_option_rows_memory_only_skips_clickhouse(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])

    class _ShouldNotQuery:
        def query(self, *_args, **_kwargs):
            raise AssertionError("clickhouse should not be queried when fallback_to_ch is False")

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _ShouldNotQuery())
    rows = query_option_rows(
        underlying="QQQ",
        right="C",
        dte_min=0,
        dte_max=0,
        ts_lb=now - timedelta(minutes=2),
        ts_now=now,
        fallback_to_ch=False,
    )
    assert len(rows) == 1
    assert rows[0]["quote_source"] == "memory"


def test_coverage_falls_back_to_clickhouse_when_memory_window_insufficient(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return [{"covered_minutes": 9, "nbbo_minutes": 7}]

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    counts = quote_minute_coverage(
        underlying="QQQ",
        window_start=now - timedelta(minutes=10),
        window_end=now,
        dte_min=0,
        dte_max=0,
        expiry=now.strftime("%Y-%m-%d"),
    )
    assert counts == {"covered_minutes": 9, "nbbo_minutes": 7}


def test_get_quote_for_opt_symbol_exposes_quote_source(monkeypatch) -> None:
    _reset_store()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    cache.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])
    opt_symbol = format_internal_opt_symbol(
        underlying="QQQ",
        expiry_ymd=now.strftime("%Y%m%d"),
        strike=500.0,
        right="C",
    )

    class _FakeCH:
        def query(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("services.market.options_quote_store.get_client", lambda: _FakeCH())
    quote = get_quote_for_opt_symbol(
        now=now,
        opt_symbol=opt_symbol,
        lookback_min=2,
        max_age_sec=90.0,
        dte_min=0,
        dte_max=0,
    )
    assert quote is not None
    assert quote["quote_source"] == "memory"
    assert validate_hotpath_quote_payload(quote) is True


def test_quote_store_hotpath_contract_is_explicit() -> None:
    contract = quote_store_hotpath_contract()
    assert contract["entry_path"]["default_fallback_to_ch"] is False
    assert contract["mark_path"]["default_fallback_to_ch"] is True
    assert contract["replay_audit_path"]["require_explicit_fallback_flag"] is True
    assert set(contract["mark_path"]["required_result_keys"]) == set(HOTPATH_QUOTE_REQUIRED_KEYS)
