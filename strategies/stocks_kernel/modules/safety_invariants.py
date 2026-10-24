from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlattenConvergenceBreach:
    symbol: str
    position_lot_id: str
    age_sec: float
    timeout_sec: float
    reason: str


class FlattenCompletionVerifier:
    """
    Track flatten requests and detect non-converged flatten actions.
    """

    def __init__(self, *, timeout_sec: float = 90.0) -> None:
        self._timeout_sec = max(30.0, float(timeout_sec or 90.0))
        self._active: dict[tuple[str, str], float] = {}

    def mark_flatten_submitted(self, *, symbol: str, position_lot_id: str, now_ts_epoch_s: float) -> None:
        symbol_u = str(symbol or "").strip().upper()
        lot_id = str(position_lot_id or "").strip()
        if not symbol_u or not lot_id:
            return
        self._active[(symbol_u, lot_id)] = float(now_ts_epoch_s or 0.0)

    def clear_symbol(self, *, symbol: str) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        keys = [key for key in self._active if key[0] == symbol_u]
        for key in keys:
            self._active.pop(key, None)

    def clear_lot(self, *, symbol: str, position_lot_id: str) -> None:
        symbol_u = str(symbol or "").strip().upper()
        lot_id = str(position_lot_id or "").strip()
        if not symbol_u or not lot_id:
            return
        self._active.pop((symbol_u, lot_id), None)

    def sweep(self, *, now_ts_epoch_s: float) -> tuple[FlattenConvergenceBreach, ...]:
        now_ts = float(now_ts_epoch_s or 0.0)
        out: list[FlattenConvergenceBreach] = []
        for (symbol, lot_id), start_ts in list(self._active.items()):
            if start_ts <= 0.0 or now_ts <= start_ts:
                continue
            age = max(0.0, now_ts - start_ts)
            if age < self._timeout_sec:
                continue
            out.append(
                FlattenConvergenceBreach(
                    symbol=symbol,
                    position_lot_id=lot_id,
                    age_sec=age,
                    timeout_sec=self._timeout_sec,
                    reason="flatten_not_converged_timeout",
                )
            )
            # Emit one timeout breach per tracked flatten request.
            self._active.pop((symbol, lot_id), None)
        return tuple(out)


@dataclass(frozen=True)
class ProtectionInvariantEvent:
    symbol: str
    position_lot_id: str
    age_sec: float
    reason: str
    severity: str
    escalate: bool


class ProtectionInvariantWatchdog:
    """
    Detect live positions that are missing protection orders.
    """

    def __init__(self, *, detect_sec: float = 10.0, escalate_sec: float = 30.0) -> None:
        self._detect_sec = max(1.0, float(detect_sec or 10.0))
        self._escalate_sec = max(self._detect_sec, float(escalate_sec or 30.0))
        self._missing_since: dict[str, float] = {}

    def evaluate(self, *, lots: tuple[Any, ...], now_ts_epoch_s: float) -> tuple[ProtectionInvariantEvent, ...]:
        now_ts = float(now_ts_epoch_s or 0.0)
        live_ids: set[str] = set()
        out: list[ProtectionInvariantEvent] = []
        for lot in lots:
            lot_id = str(getattr(lot, "position_lot_id", "") or "").strip()
            if not lot_id:
                continue
            state = str(getattr(lot, "state", "") or "").strip().lower()
            qty = float(getattr(lot, "live_qty", 0.0) or 0.0)
            if state not in {"live", "pending_exit"} or qty <= 0.0:
                continue
            live_ids.add(lot_id)
            has_protection = bool(tuple(getattr(lot, "protection_order_refs", ()) or ()))
            flattening = bool(tuple(getattr(lot, "flatten_order_refs", ()) or ()))
            if has_protection or flattening:
                self._missing_since.pop(lot_id, None)
                continue
            start_ts = self._missing_since.get(lot_id)
            if start_ts is None:
                self._missing_since[lot_id] = now_ts
                start_ts = now_ts
            age_sec = max(0.0, now_ts - float(start_ts or now_ts))
            if age_sec < self._detect_sec:
                continue
            escalate = age_sec >= self._escalate_sec
            out.append(
                ProtectionInvariantEvent(
                    symbol=str(getattr(lot, "symbol", "") or "").strip().upper(),
                    position_lot_id=lot_id,
                    age_sec=age_sec,
                    reason="live_position_missing_protection",
                    severity="error" if escalate else "warning",
                    escalate=escalate,
                )
            )

        stale = [lot_id for lot_id in self._missing_since if lot_id not in live_ids]
        for lot_id in stale:
            self._missing_since.pop(lot_id, None)
        return tuple(out)
