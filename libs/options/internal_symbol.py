from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class InternalOptionSymbol:
    underlying: str
    expiry_ymd: str  # YYYYMMDD
    strike: float
    right: str  # C/P


def is_internal_opt_symbol(symbol: str) -> bool:
    return str(symbol or "").upper().startswith("OPT:")


def _norm_underlying(underlying: str) -> str:
    u = str(underlying or "").strip().upper()
    if not u:
        raise ValueError("underlying empty")
    if ":" in u:
        raise ValueError(f"bad underlying: {u}")
    return u


def _norm_right(right: str) -> str:
    r = str(right or "").strip().upper()
    if r in {"CALL", "C"}:
        return "C"
    if r in {"PUT", "P"}:
        return "P"
    raise ValueError(f"bad right: {right}")


def _norm_expiry_ymd(expiry_ymd: str) -> str:
    s = str(expiry_ymd or "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"bad expiry_ymd: {expiry_ymd}")
    return s


def _fmt_strike(strike: float) -> str:
    try:
        x = float(strike)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"bad strike: {strike}") from exc
    if not (x > 0):
        raise ValueError(f"bad strike: {strike}")
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s


def format_internal_opt_symbol(*, underlying: str, expiry_ymd: str, strike: float, right: str) -> str:
    u = _norm_underlying(underlying)
    e = _norm_expiry_ymd(expiry_ymd)
    r = _norm_right(right)
    k = _fmt_strike(strike)
    return f"OPT:{u}:{e}:{k}:{r}"


def parse_internal_opt_symbol(symbol: str) -> InternalOptionSymbol:
    s = str(symbol or "").strip().upper()
    parts = s.split(":")
    if len(parts) != 5 or parts[0] != "OPT":
        raise ValueError(f"not internal OPT symbol: {symbol}")
    underlying = _norm_underlying(parts[1])
    expiry_ymd = _norm_expiry_ymd(parts[2])
    try:
        strike = float(parts[3])
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"bad strike in symbol: {symbol}") from exc
    right = _norm_right(parts[4])
    return InternalOptionSymbol(
        underlying=underlying,
        expiry_ymd=expiry_ymd,
        strike=float(strike),
        right=right,
    )


def internal_opt_symbol_from_ib_contract(contract: Any) -> str | None:
    """
    Best-effort mapping from an IB contract to internal OPT:... symbol.

    IMPORTANT:
    - IB option contracts have `contract.symbol` = underlying (e.g. QQQ)
    - We must include expiry/strike/right to avoid collisions.
    """
    if contract is None:
        return None
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    if sec_type != "OPT":
        return None

    underlying = getattr(contract, "symbol", None)
    expiry = getattr(contract, "lastTradeDateOrContractMonth", None)
    strike = getattr(contract, "strike", None)
    right = getattr(contract, "right", None)
    if not (underlying and expiry and strike and right):
        return None

    expiry_ymd = str(expiry).strip()
    # ib_insync sometimes returns YYYYMMDD, sometimes YYYY-MM-DD; normalize to YYYYMMDD.
    expiry_ymd = expiry_ymd.replace("-", "")
    try:
        return format_internal_opt_symbol(
            underlying=str(underlying),
            expiry_ymd=expiry_ymd,
            strike=float(strike),
            right=str(right),
        )
    except Exception:
        return None


def ib_option_contract_from_internal_symbol(symbol: str) -> Any:
    """
    Convert internal OPT:... symbol into an ib_insync.Option Contract.

    Kept as a late import to avoid making unit tests depend on ib_insync.
    """
    parts = parse_internal_opt_symbol(symbol)
    try:
        from ib_insync import Option  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("ib_insync not available") from exc

    return Option(
        symbol=parts.underlying,
        lastTradeDateOrContractMonth=parts.expiry_ymd,
        strike=float(parts.strike),
        right=parts.right,
        exchange="SMART",
        currency="USD",
        multiplier="100",
    )


def _fake_opt_contract_for_tests(
    *,
    underlying: str = "QQQ",
    expiry_ymd: str = "20260305",
    strike: float = 430.0,
    right: str = "C",
) -> Any:
    return SimpleNamespace(
        secType="OPT",
        symbol=underlying,
        lastTradeDateOrContractMonth=expiry_ymd,
        strike=strike,
        right=right,
    )

