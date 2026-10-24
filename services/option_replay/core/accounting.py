"""Cash accounting helpers for the option replay engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def record_cash_ledger(
    engine: Any,
    ts: datetime,
    *,
    event: str,
    amount: float,
    fees: float,
    cash_after: float,
    order_id: str,
    side: str,
    qty: int,
    option_price: float,
) -> None:
    engine.cash_ledger.append(
        {
            "ts": ts.isoformat(),
            "event": event,
            "amount": float(amount),
            "fees": float(fees),
            "cash_after": float(cash_after),
            "order_id": order_id,
            "side": side,
            "qty": int(qty),
            "option_price": float(option_price),
        }
    )


def apply_entry_fill(engine: Any, ts: datetime, *, order_id: str, qty: int, fill_px: float, side: str) -> float:
    gross = qty * fill_px * engine.cfg.contract_multiplier
    fees = qty * engine.cfg.commission_per_contract
    engine.cash -= gross + fees
    record_cash_ledger(
        engine,
        ts,
        event="ENTRY_FILL",
        amount=-(gross + fees),
        fees=fees,
        cash_after=engine.cash,
        order_id=order_id,
        side=side,
        qty=qty,
        option_price=fill_px,
    )
    return fees


def apply_exit_fill(engine: Any, ts: datetime, *, order_id: str, qty: int, fill_px: float, side: str) -> float:
    gross = qty * fill_px * engine.cfg.contract_multiplier
    fees = qty * engine.cfg.commission_per_contract
    engine.cash += gross - fees
    record_cash_ledger(
        engine,
        ts,
        event="EXIT_FILL",
        amount=gross - fees,
        fees=fees,
        cash_after=engine.cash,
        order_id=order_id,
        side=side,
        qty=qty,
        option_price=fill_px,
    )
    return fees


__all__ = [
    "apply_entry_fill",
    "apply_exit_fill",
    "record_cash_ledger",
]
