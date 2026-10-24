import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import tools.ingest_options_quotes as ingest_mod
from services.market.options_data import OptionContract


ET = ZoneInfo("America/New_York")


def test_build_rows_uses_minute_bucket_ts():
    contract = OptionContract(
        symbol="QQQ_2026-03-05_C_600",
        underlying="QQQ",
        expiry_date="2026-03-05",
        strike=600.0,
        right="C",
        bid=1.0,
        ask=1.2,
        last=1.1,
        volume=10,
        open_interest=100,
        delta=0.5,
        gamma=0.01,
        vega=0.2,
        theta=-0.1,
        implied_vol=0.25,
    )

    rows = ingest_mod._build_rows([contract], "ibkr")
    assert len(rows) == 1
    ts = rows[0][0]
    created_at = rows[0][-1]

    assert ts.tzinfo is not None
    assert ts.second == 0
    assert ts.microsecond == 0
    assert created_at.tzinfo is not None
    assert created_at >= ts


@pytest.mark.asyncio
async def test_run_skips_insert_when_bucket_already_exists(monkeypatch):
    service = ingest_mod.OptionQuotesIngestService(
        provider="ibkr",
        symbols=["QQQ"],
        dte=[0],
        poll_sec=1,
    )
    calls = {"fetch": 0}

    async def fake_start():
        service._provider = object()

    async def fake_fetch(*args, **kwargs):
        calls["fetch"] += 1
        return 16, [{"underlying": "QQQ", "contracts_total": 16}]

    async def fake_sleep(_):
        service._running = False

    monkeypatch.setattr(service, "_is_rth", lambda: True)
    monkeypatch.setattr(service, "_bucket_has_rows", lambda bucket: True)
    monkeypatch.setattr(service, "start", fake_start)
    monkeypatch.setattr(ingest_mod, "fetch_and_insert_quotes", fake_fetch)
    monkeypatch.setattr(ingest_mod.asyncio, "sleep", fake_sleep)

    await service.run()
    assert calls["fetch"] == 0


@pytest.mark.asyncio
async def test_run_inserts_at_most_once_per_bucket(monkeypatch):
    service = ingest_mod.OptionQuotesIngestService(
        provider="ibkr",
        symbols=["QQQ"],
        dte=[0],
        poll_sec=1,
    )
    calls = {"fetch": 0, "sleep": 0}

    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 3, 5, 10, 30, 15, tzinfo=ET)
            if tz is None:
                return base.replace(tzinfo=None)
            return base.astimezone(tz)

    async def fake_start():
        service._provider = object()

    async def fake_fetch(*args, **kwargs):
        calls["fetch"] += 1
        return 16, [{"underlying": "QQQ", "contracts_total": 16}]

    async def fake_sleep(_):
        calls["sleep"] += 1
        if calls["sleep"] >= 2:
            service._running = False

    monkeypatch.setattr(ingest_mod, "datetime", _FixedDateTime)
    monkeypatch.setattr(service, "_is_rth", lambda: True)
    monkeypatch.setattr(service, "_bucket_has_rows", lambda bucket: False)
    monkeypatch.setattr(service, "start", fake_start)
    monkeypatch.setattr(ingest_mod, "fetch_and_insert_quotes", fake_fetch)
    monkeypatch.setattr(ingest_mod.asyncio, "sleep", fake_sleep)

    await service.run()
    assert calls["fetch"] == 1


@pytest.mark.asyncio
async def test_run_backs_off_when_fetch_returns_zero_rows(monkeypatch):
    service = ingest_mod.OptionQuotesIngestService(
        provider="ibkr",
        symbols=["QQQ"],
        dte=[0],
        poll_sec=1,
    )

    async def fake_start():
        service._provider = object()

    async def fake_fetch(*args, **kwargs):
        return 0, [{"underlying": "QQQ", "contracts_total": 0}]

    async def fake_sleep(_):
        service._running = False

    monkeypatch.setattr(service, "_is_rth", lambda: True)
    monkeypatch.setattr(service, "_bucket_has_rows", lambda bucket: False)
    monkeypatch.setattr(service, "start", fake_start)
    monkeypatch.setattr(ingest_mod, "fetch_and_insert_quotes", fake_fetch)
    monkeypatch.setattr(ingest_mod.asyncio, "sleep", fake_sleep)

    await service.run()
    assert service._failure_streak == 1


def test_instance_lock_blocks_second_process(tmp_path, monkeypatch):
    lock_path = tmp_path / "opt_ingest.lock"
    monkeypatch.setenv("OPTIONS_QUOTES_LOCK_PATH", str(lock_path))

    s1 = ingest_mod.OptionQuotesIngestService(symbols=["QQQ"], dte=[0])
    s2 = ingest_mod.OptionQuotesIngestService(symbols=["QQQ"], dte=[0])

    s1._acquire_instance_lock()
    try:
        with pytest.raises(RuntimeError):
            s2._acquire_instance_lock()
    finally:
        s1._release_instance_lock()


@pytest.mark.asyncio
async def test_start_releases_lock_when_provider_init_fails(tmp_path, monkeypatch):
    lock_path = tmp_path / "opt_ingest.lock"
    monkeypatch.setenv("OPTIONS_QUOTES_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(ingest_mod, "ensure_options_quotes_table", lambda: None)
    monkeypatch.setattr(ingest_mod, "create_options_provider", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    service = ingest_mod.OptionQuotesIngestService(symbols=["QQQ"], dte=[0])
    with pytest.raises(RuntimeError, match="boom"):
        await service.start()

    assert service._lock_fp is None

    s2 = ingest_mod.OptionQuotesIngestService(symbols=["QQQ"], dte=[0])
    s2._acquire_instance_lock()
    s2._release_instance_lock()


def test_bucket_has_rows_requires_minimum_symbol_side_coverage(monkeypatch):
    bucket = datetime(2026, 3, 5, 10, 30, tzinfo=ET)
    service = ingest_mod.OptionQuotesIngestService(
        provider="ibkr",
        symbols=["QQQ", "SPY"],
        dte=[0],
        right=None,
    )

    responses = [
        [{"c": 1, "u_sym": 1, "u_side": 1}],  # partial
        [{"c": 4, "u_sym": 2, "u_side": 4}],  # complete for 2 symbols x 2 sides
    ]

    class _FakeClient:
        def query(self, _q, parameters=None):
            assert parameters is not None
            assert parameters["source"] == "ibkr"
            return responses.pop(0)

    monkeypatch.setattr(ingest_mod, "get_client", lambda: _FakeClient())

    assert service._bucket_has_rows(bucket) is False
    assert service._bucket_has_rows(bucket) is True


def test_ensure_options_quotes_table_executes_ddl(monkeypatch):
    called = {"sql": None}

    class _FakeClient:
        def command(self, sql):
            called["sql"] = sql

    monkeypatch.setattr(ingest_mod, "get_client", lambda: _FakeClient())
    ingest_mod.ensure_options_quotes_table()
    assert called["sql"] is not None
    assert "CREATE TABLE IF NOT EXISTS trading.options_quotes_1m" in called["sql"]


def test_insert_rows_updates_memory_only_after_insert(monkeypatch):
    order = []

    class _FakeClient:
        def insert(self, *_args, **_kwargs):
            order.append("insert")

    monkeypatch.setattr(ingest_mod, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(ingest_mod, "update_from_ingest_rows", lambda rows: order.append("cache"))

    now = datetime(2026, 3, 5, 10, 30, tzinfo=ET)
    rows = [
        (
            now,
            "QQQ",
            now.date(),
            600.0,
            "C",
            0,
            1.0,
            1.2,
            1.1,
            1.1,
            0,
            0,
            0.2,
            0.5,
            0.01,
            -0.01,
            0.02,
            10.0,
            100,
            "ibkr",
            now + timedelta(seconds=5),
        )
    ]

    inserted = ingest_mod._insert_rows(rows)
    assert inserted == 1
    assert order == ["insert", "cache"]


@pytest.mark.asyncio
async def test_service_start_ensures_table_before_provider(monkeypatch):
    order = []
    service = ingest_mod.OptionQuotesIngestService(provider="stub", symbols=["QQQ"], dte=[0])

    class _Provider:
        async def disconnect(self):
            return None

    def _fake_ensure():
        order.append("ensure")

    def _fake_provider(*args, **kwargs):
        order.append("provider")
        return _Provider()

    monkeypatch.setattr(ingest_mod, "ensure_options_quotes_table", _fake_ensure)
    monkeypatch.setattr(ingest_mod, "create_options_provider", _fake_provider)
    monkeypatch.setattr(service, "_acquire_instance_lock", lambda: None)
    monkeypatch.setattr(service, "_release_instance_lock", lambda: None)

    await service.start()
    await service.stop()
    assert order[:2] == ["ensure", "provider"]


def test_summarize_contracts_reports_dte0_and_nbbo():
    future_expiry = (datetime.now(ET).date() + timedelta(days=7)).isoformat()
    contracts = [
        OptionContract(
            symbol="QQQ_2026-03-12_C_600",
            underlying="QQQ",
            expiry_date=datetime.now(ET).date().isoformat(),
            strike=600.0,
            right="C",
            bid=1.0,
            ask=1.2,
            last=1.1,
        ),
        OptionContract(
            symbol="QQQ_2026-03-19_P_590",
            underlying="QQQ",
            expiry_date=future_expiry,
            strike=590.0,
            right="P",
            bid=0.0,
            ask=0.0,
            last=0.5,
        ),
    ]
    summary = ingest_mod._summarize_contracts("QQQ", contracts, [0, 7], None)
    assert summary["underlying"] == "QQQ"
    assert summary["contracts_total"] == 2
    assert summary["contracts_dte0"] == 1
    assert summary["contracts_expiry_today"] == 1
    assert summary["contracts_with_nbbo"] == 1
