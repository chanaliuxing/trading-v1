from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Optional

from strategies.stocks_kernel.types import KernelSymbolExposureView, PositionLot, PositionLotStatus


def _safe_positive(value: float) -> float:
    value_f = float(value)
    return max(0.0, value_f)


class PositionLotLedger:
    """
    Lot-level truth store for same-symbol multi-strategy live positions.

    Symbol views are derived from lots and are never treated as an
    independent source of truth.
    """

    def __init__(self) -> None:
        self._lots: dict[str, PositionLot] = {}

    def open_lot(self, lot: PositionLot) -> PositionLot:
        if lot.position_lot_id in self._lots:
            raise ValueError(f"duplicate lot id: {lot.position_lot_id}")
        self._lots[lot.position_lot_id] = replace(lot)
        return self._lots[lot.position_lot_id]

    def get_lot(self, position_lot_id: str) -> Optional[PositionLot]:
        lot = self._lots.get(str(position_lot_id))
        return replace(lot) if lot is not None else None

    def find_lots(
        self,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        entry_order_ref: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
        include_closed: bool = False,
    ) -> list[PositionLot]:
        out: list[PositionLot] = []
        symbol_u = str(symbol or "").strip().upper() or None
        strategy_id_v = str(strategy_id or "").strip() or None
        entry_order_ref_v = str(entry_order_ref or "").strip() or None
        broker_order_id_v = str(broker_order_id or "").strip() or None
        perm_id_v = int(perm_id) if perm_id is not None else None
        for lot in self._lots.values():
            if strategy_id_v and lot.strategy_id != strategy_id_v:
                continue
            if symbol_u and lot.symbol != symbol_u:
                continue
            if entry_order_ref_v and lot.entry_order_ref != entry_order_ref_v:
                continue
            if broker_order_id_v and lot.entry_order_id != broker_order_id_v:
                continue
            if perm_id_v is not None and lot.entry_perm_id != perm_id_v:
                continue
            if not include_closed and lot.state == PositionLotStatus.CLOSED:
                continue
            out.append(replace(lot))
        return out

    def list_lots(
        self,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        include_closed: bool = False,
    ) -> list[PositionLot]:
        out: list[PositionLot] = []
        symbol_u = str(symbol or "").strip().upper() or None
        strategy_id_v = str(strategy_id or "").strip() or None
        for lot in self._lots.values():
            if strategy_id_v and lot.strategy_id != strategy_id_v:
                continue
            if symbol_u and lot.symbol != symbol_u:
                continue
            if not include_closed and lot.state == PositionLotStatus.CLOSED:
                continue
            out.append(replace(lot))
        return out

    def record_entry_fill(
        self,
        *,
        position_lot_id: str,
        fill_qty: float,
        fill_price: float,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        fill_qty_f = _safe_positive(fill_qty)
        if fill_qty_f <= 0:
            raise ValueError("fill_qty must be positive")
        total_qty = lot.filled_qty + fill_qty_f
        weighted_cost = 0.0
        if lot.avg_entry_price is not None:
            weighted_cost += lot.avg_entry_price * lot.filled_qty
        weighted_cost += float(fill_price) * fill_qty_f
        lot.filled_qty = total_qty
        lot.live_qty = total_qty
        lot.avg_entry_price = weighted_cost / total_qty if total_qty > 0 else None
        lot.state = PositionLotStatus.LIVE
        if broker_order_id:
            lot.entry_order_id = str(broker_order_id)
        if perm_id is not None:
            lot.entry_perm_id = int(perm_id)
        return replace(lot)

    def attach_entry_identity(
        self,
        *,
        position_lot_id: str,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        if broker_order_id:
            lot.entry_order_id = str(broker_order_id)
        if perm_id is not None:
            lot.entry_perm_id = int(perm_id)
        return replace(lot)

    def request_exit(self, *, position_lot_id: str, flatten_order_ref: str) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        if flatten_order_ref not in lot.flatten_order_refs:
            lot.flatten_order_refs.append(str(flatten_order_ref))
        if lot.state != PositionLotStatus.CLOSED:
            lot.state = PositionLotStatus.PENDING_EXIT
        return replace(lot)

    def record_exit_fill(self, *, position_lot_id: str, fill_qty: float) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        fill_qty_f = _safe_positive(fill_qty)
        if fill_qty_f <= 0:
            raise ValueError("fill_qty must be positive")
        remaining = max(0.0, lot.live_qty - fill_qty_f)
        lot.live_qty = remaining
        # Exit order refs represent in-flight exit work; once any exit fill arrives we
        # clear them so post-partial lifecycle management (TP1 -> trailing, etc.) can continue.
        lot.flatten_order_refs = []
        if remaining <= 0:
            lot.state = PositionLotStatus.CLOSED
        else:
            lot.state = PositionLotStatus.LIVE
        return replace(lot)

    def attach_protection_order(self, *, position_lot_id: str, protection_order_ref: str) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        ref = str(protection_order_ref).strip()
        if not ref:
            raise ValueError("protection_order_ref is required")
        if ref not in lot.protection_order_refs:
            lot.protection_order_refs.append(ref)
        return replace(lot)

    def patch_lot_extras(self, *, position_lot_id: str, updates: dict[str, object]) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        lot.extras.update(dict(updates or {}))
        return replace(lot)

    def reconcile_broker_qty(
        self,
        *,
        position_lot_id: str,
        live_qty: float,
        avg_entry_price: Optional[float] = None,
    ) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        live_qty_f = _safe_positive(live_qty)
        lot.live_qty = live_qty_f
        lot.filled_qty = max(float(lot.filled_qty or 0.0), live_qty_f)
        if avg_entry_price is not None:
            lot.avg_entry_price = float(avg_entry_price)
        if live_qty_f <= 0.0:
            lot.state = PositionLotStatus.CLOSED
        elif lot.state != PositionLotStatus.QUARANTINED:
            lot.state = PositionLotStatus.LIVE
        return replace(lot)

    def quarantine_lot(self, *, position_lot_id: str, reason: str) -> PositionLot:
        lot = self._require_lot(position_lot_id)
        lot.state = PositionLotStatus.QUARANTINED
        lot.extras["quarantine_reason"] = str(reason or "").strip() or "quarantined"
        return replace(lot)

    def symbol_view(self, *, strategy_id: str, symbol: str) -> KernelSymbolExposureView:
        long_qty = 0.0
        short_qty = 0.0
        open_lot_count = 0
        for lot in self._iter_open_lots(strategy_id=strategy_id, symbol=symbol):
            open_lot_count += 1
            if lot.side.upper() == "LONG":
                long_qty += lot.live_qty
            else:
                short_qty += lot.live_qty
        return KernelSymbolExposureView(
            strategy_id=str(strategy_id),
            symbol=str(symbol).strip().upper(),
            long_qty=long_qty,
            short_qty=short_qty,
            net_qty=long_qty - short_qty,
            open_lot_count=open_lot_count,
        )

    def _iter_open_lots(self, *, strategy_id: str, symbol: str) -> Iterable[PositionLot]:
        strategy_id_v = str(strategy_id).strip()
        symbol_u = str(symbol).strip().upper()
        for lot in self._lots.values():
            if lot.strategy_id != strategy_id_v or lot.symbol != symbol_u:
                continue
            if lot.state == PositionLotStatus.CLOSED:
                continue
            yield lot

    def _require_lot(self, position_lot_id: str) -> PositionLot:
        key = str(position_lot_id)
        lot = self._lots.get(key)
        if lot is None:
            raise KeyError(f"unknown lot id: {key}")
        return lot
