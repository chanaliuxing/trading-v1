from __future__ import annotations

from scripts.ai_trader.gates import realtime_gate as rg


def test_check_no_symbols_ok_true():
    res = rg.check([], redis_url="redis://invalid:6379/0", max_age_sec=60, min_ok_ratio=0.8)
    assert res.get("ok") is True
    assert res.get("reason") == "no_symbols"


def test_check_invalid_redis_returns_not_ok():
    # With at least one symbol, invalid redis should yield not ok
    res = rg.check(["TSLA"], redis_url="redis://127.0.0.1:1/0", max_age_sec=60, min_ok_ratio=0.8)
    assert res.get("ok") is False
