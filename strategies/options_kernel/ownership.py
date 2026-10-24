from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .types import KernelIntentRecord, PositionLot


_ET = ZoneInfo("America/New_York")


class OptionOwnershipLedger:
    """
    Strategy-scoped in-memory ownership projection for v2 option strategies.

    This intentionally stays isolated from existing shared execution/reconciliation
    defaults. It tracks v2 lots and order refs without mutating legacy state.
    """

    def __init__(self) -> None:
        self._lots: Dict[str, PositionLot] = {}
        self._order_to_lot: Dict[str, str] = {}
        self._intents: List[KernelIntentRecord] = []
        self._conflicts: List[Dict[str, Any]] = []

    def record_intent(self, record: KernelIntentRecord) -> None:
        self._intents.append(record)
        if record.target_qty > 0:
            lot = self._lots.get(record.position_lot_id)
            side = str(record.payload.get("side") or "LONG")
            if lot is None:
                self._lots[record.position_lot_id] = PositionLot(
                    strategy_id=record.strategy_id,
                    position_lot_id=record.position_lot_id,
                    symbol=record.symbol,
                    side=side,
                    qty=float(record.target_qty),
                    opened_at=datetime.fromtimestamp(record.ts_bar_end_us / 1_000_000.0, tz=_ET),
                    entry_decision_id=record.decision_id,
                    entry_trigger_id=str(record.payload.get("trigger_id") or ""),
                    entry_snapshot_id=record.decision_snapshot_id,
                    execution_profile_id=record.execution_profile_id,
                    order_ref=record.order_ref,
                    status="intent_open",
                    payload=dict(record.payload or {}),
                )
            else:
                lot.side = side or lot.side
                lot.payload.update(dict(record.payload or {}))
            if record.order_ref:
                self._order_to_lot[str(record.order_ref)] = record.position_lot_id
        elif record.target_qty < 0 and record.position_lot_id:
            lot = self._lots.get(record.position_lot_id)
            if lot is None:
                self._conflicts.append(
                    {
                        "type": "missing_lot_for_exit",
                        "position_lot_id": str(record.position_lot_id),
                        "strategy_id": str(record.strategy_id),
                        "order_ref": str(record.order_ref),
                    }
                )
                return
            lot.payload.update(dict(record.payload or {}))
            lot.payload["pending_exit_qty"] = float(abs(record.target_qty))
            lot.payload["pending_exit_decision_id"] = str(record.decision_id)
            lot.status = "pending_exit"
            if record.order_ref:
                self._order_to_lot[str(record.order_ref)] = record.position_lot_id

    def resolve_entry_intent(self, *, position_lot_id: str, accepted: bool, payload: Optional[Dict[str, Any]] = None) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "resolve_unknown_entry_lot",
                    "position_lot_id": str(position_lot_id),
                    "accepted": bool(accepted),
                }
            )
            return
        if payload:
            lot.payload.update(dict(payload))
        if accepted:
            lot.status = "pending_fill"
            return
        lot.status = "rejected"
        lot.qty = 0.0

    def mark_entry_live(
        self,
        *,
        position_lot_id: str,
        filled_qty: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "mark_unknown_entry_lot_live",
                    "position_lot_id": str(position_lot_id),
                }
            )
            return
        if payload:
            lot.payload.update(dict(payload))
        if filled_qty is not None:
            lot.qty = max(0.0, float(filled_qty))
        lot.status = "live" if lot.qty > 0.0 else "closed"

    def resolve_exit_intent(
        self,
        *,
        position_lot_id: str,
        accepted: bool,
        exit_qty: float,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "resolve_unknown_exit_lot",
                    "position_lot_id": str(position_lot_id),
                    "accepted": bool(accepted),
                    "exit_qty": float(exit_qty),
                }
            )
            return
        if payload:
            lot.payload.update(dict(payload))
        if not accepted:
            lot.payload.pop("pending_exit_qty", None)
            lot.payload.pop("pending_exit_decision_id", None)
            lot.status = "live"
            return
        lot.payload["pending_exit_qty"] = max(0.0, float(exit_qty))
        lot.status = "pending_exit"

    def reconcile_symbol_live_qty(
        self,
        *,
        strategy_id: str,
        symbol: str,
        live_qty: float,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        sid = str(strategy_id)
        sym = str(symbol)
        target_qty = max(0.0, float(live_qty))
        lots = [
            lot
            for lot in self._lots.values()
            if str(lot.strategy_id) == sid and str(lot.symbol) == sym
        ]
        if not lots:
            if target_qty > 0.0:
                self._conflicts.append(
                    {
                        "type": "reconcile_unknown_symbol_live_qty",
                        "strategy_id": sid,
                        "symbol": sym,
                        "live_qty": target_qty,
                    }
                )
            return
        lots = sorted(lots, key=lambda row: row.opened_at, reverse=True)
        primary = lots[0]
        if payload:
            primary.payload.update(dict(payload))
        primary.payload.pop("pending_exit_qty", None)
        primary.payload.pop("pending_exit_decision_id", None)
        primary.qty = float(target_qty)
        primary.status = "live" if target_qty > 0.0 else "closed"
        for lot in lots[1:]:
            lot.qty = 0.0
            lot.status = "closed"
            lot.payload.pop("pending_exit_qty", None)
            lot.payload.pop("pending_exit_decision_id", None)

    def attach_order_ref(self, *, position_lot_id: str, order_ref: str) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "attach_unknown_lot",
                    "position_lot_id": str(position_lot_id),
                    "order_ref": str(order_ref),
                }
            )
            return
        self._order_to_lot[str(order_ref)] = str(position_lot_id)
        lot.order_ref = str(order_ref)

    def lot_for_order_ref(self, order_ref: str) -> Optional[PositionLot]:
        lot_id = self._order_to_lot.get(str(order_ref))
        if not lot_id:
            return None
        return self._lots.get(lot_id)

    def active_lots(self, *, strategy_id: Optional[str] = None, symbol: Optional[str] = None) -> List[PositionLot]:
        out: List[PositionLot] = []
        for lot in self._lots.values():
            if strategy_id and lot.strategy_id != str(strategy_id):
                continue
            if symbol and lot.symbol != str(symbol):
                continue
            if float(lot.qty) <= 0.0:
                continue
            if str(lot.status or "") not in {"live", "partial_exit", "pending_exit"}:
                continue
            out.append(lot)
        return sorted(out, key=lambda x: (x.strategy_id, x.symbol, x.opened_at.isoformat(), x.position_lot_id))

    def pending_entry_lots(self, *, strategy_id: Optional[str] = None, symbol: Optional[str] = None) -> List[PositionLot]:
        out: List[PositionLot] = []
        for lot in self._lots.values():
            if strategy_id and lot.strategy_id != str(strategy_id):
                continue
            if symbol and lot.symbol != str(symbol):
                continue
            if float(lot.qty) <= 0.0:
                continue
            if str(lot.status or "") not in {"intent_open", "pending_fill"}:
                continue
            out.append(lot)
        return sorted(out, key=lambda x: (x.strategy_id, x.symbol, x.opened_at.isoformat(), x.position_lot_id))

    def conflicts(self) -> List[Dict[str, Any]]:
        return list(self._conflicts)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "lots": [asdict(lot) for lot in self._lots.values()],
            "order_to_lot": dict(self._order_to_lot),
            "intent_count": len(self._intents),
            "conflicts": list(self._conflicts),
        }
