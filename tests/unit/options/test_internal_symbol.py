from __future__ import annotations

from types import SimpleNamespace

import pytest

from libs.options.internal_symbol import (
    format_internal_opt_symbol,
    internal_opt_symbol_from_ib_contract,
    is_internal_opt_symbol,
    parse_internal_opt_symbol,
    _fake_opt_contract_for_tests,
)


def test_format_parse_roundtrip_int_strike() -> None:
    sym = format_internal_opt_symbol(underlying="QQQ", expiry_ymd="20260305", strike=430.0, right="C")
    assert sym == "OPT:QQQ:20260305:430:C"
    parts = parse_internal_opt_symbol(sym)
    assert parts.underlying == "QQQ"
    assert parts.expiry_ymd == "20260305"
    assert parts.right == "C"
    assert abs(parts.strike - 430.0) < 1e-9
    assert is_internal_opt_symbol(sym)


def test_format_parse_roundtrip_half_strike() -> None:
    sym = format_internal_opt_symbol(underlying="QQQ", expiry_ymd="20260305", strike=430.5, right="P")
    assert sym == "OPT:QQQ:20260305:430.5:P"
    parts = parse_internal_opt_symbol(sym)
    assert abs(parts.strike - 430.5) < 1e-9
    assert parts.right == "P"


def test_internal_symbol_from_ib_contract_opt() -> None:
    c = _fake_opt_contract_for_tests(underlying="QQQ", expiry_ymd="20260305", strike=430.0, right="C")
    sym = internal_opt_symbol_from_ib_contract(c)
    assert sym == "OPT:QQQ:20260305:430:C"


def test_internal_symbol_from_ib_contract_non_opt() -> None:
    c = SimpleNamespace(secType="STK", symbol="QQQ")
    assert internal_opt_symbol_from_ib_contract(c) is None


def test_parse_rejects_bad_symbols() -> None:
    with pytest.raises(ValueError):
        parse_internal_opt_symbol("QQQ")
    with pytest.raises(ValueError):
        parse_internal_opt_symbol("OPT:QQQ:20260305:BAD:C")
    with pytest.raises(ValueError):
        parse_internal_opt_symbol("OPT:QQQ:2026-03-05:430:C")

