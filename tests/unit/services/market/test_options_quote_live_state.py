from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def _reload_live_state(monkeypatch, backend: str):
    monkeypatch.setenv("OPTIONS_QUOTE_LIVE_STATE_BACKEND", backend)
    import services.market.options_quote_live_state as mod

    return importlib.reload(mod)


def test_live_state_memory_backend_update_and_query(monkeypatch):
    mod = _reload_live_state(monkeypatch, "memory")
    now = datetime.now(ET).replace(second=0, microsecond=0)
    mod.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])
    rows = mod.query_rows(
        underlying="QQQ",
        right="C",
        dte_min=0,
        dte_max=0,
        ts_lb=now - timedelta(minutes=2),
        ts_now=now,
    )
    assert len(rows) == 1
    assert rows[0]["quote_source"] in {"live_state_memory", "live_state"}


def test_live_state_off_backend_is_noop(monkeypatch):
    mod = _reload_live_state(monkeypatch, "off")
    now = datetime.now(ET).replace(second=0, microsecond=0)
    mod.update_from_ingest_rows([_mk_row(now - timedelta(minutes=1), "QQQ", 500.0, "C")])
    rows = mod.query_rows(
        underlying="QQQ",
        right="C",
        dte_min=0,
        dte_max=0,
        ts_lb=now - timedelta(minutes=2),
        ts_now=now,
    )
    assert rows == []
