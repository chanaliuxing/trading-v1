from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from strategies.index_super_win_replica_ownership_types import (
    ReplicaExitFillRecord,
    ReplicaLotRecord,
    ReplicaOwnershipIntentRecord,
)


class StockOwnershipLedger:
    """
    Strategy-owned equity lot ledger for index_super_win replica.

    This is intentionally local and strategy-scoped. It does not rely on
    broker net inventory as the primary ownership source.
    """

    def __init__(self) -> None:
        self._lots: Dict[str, ReplicaLotRecord] = {}
        self._order_to_lot: Dict[str, str] = {}
        self._broker_to_lot: Dict[str, str] = {}
        self._perm_to_lot: Dict[int, str] = {}
        self._intents: List[ReplicaOwnershipIntentRecord] = []
        self._exit_fills: List[ReplicaExitFillRecord] = []
        self._conflicts: List[Dict[str, Any]] = []

    def record_entry_intent(self, record: ReplicaOwnershipIntentRecord) -> None:
        self._intents.append(record)
        lot = self._lots.get(record.position_lot_id)
        opened_at = datetime.fromtimestamp(record.ts_bar_end_us / 1_000_000.0)
        if lot is None:
            lot = ReplicaLotRecord(
                strategy_id=record.strategy_id,
                position_lot_id=record.position_lot_id,
                signal_symbol=record.signal_symbol,
                exec_symbol=record.exec_symbol,
                side=record.side,
                planned_qty=float(record.qty),
                live_qty=0.0,
                remaining_qty=0.0,
                opened_at=opened_at,
                entry_decision_id=record.decision_id,
                entry_trigger_id=record.trigger_id,
                entry_order_ref=record.order_ref,
                status="intent_open",
                payload=dict(record.payload or {}),
            )
            self._lots[record.position_lot_id] = lot
        else:
            lot.planned_qty = max(float(lot.planned_qty), float(record.qty))
            lot.entry_order_ref = str(record.order_ref or lot.entry_order_ref)
            lot.payload.update(dict(record.payload or {}))
        if record.order_ref:
            self._order_to_lot[str(record.order_ref)] = record.position_lot_id

    @classmethod
    def from_snapshot(cls, payload: Dict[str, Any] | None) -> "StockOwnershipLedger":
        ledger = cls()
        if not isinstance(payload, dict):
            return ledger
        for raw in list(payload.get("lots") or []):
            if not isinstance(raw, dict):
                continue
            opened_at = raw.get("opened_at")
            try:
                opened_at_dt = datetime.fromisoformat(str(opened_at)) if opened_at else datetime.utcnow()
            except Exception:
                opened_at_dt = datetime.utcnow()
            lot = ReplicaLotRecord(
                strategy_id=str(raw.get("strategy_id") or ""),
                position_lot_id=str(raw.get("position_lot_id") or ""),
                signal_symbol=str(raw.get("signal_symbol") or ""),
                exec_symbol=str(raw.get("exec_symbol") or ""),
                side=str(raw.get("side") or ""),
                planned_qty=float(raw.get("planned_qty") or 0.0),
                live_qty=float(raw.get("live_qty") or 0.0),
                remaining_qty=float(raw.get("remaining_qty") or 0.0),
                opened_at=opened_at_dt,
                entry_decision_id=str(raw.get("entry_decision_id") or ""),
                entry_trigger_id=str(raw.get("entry_trigger_id") or ""),
                entry_order_ref=str(raw.get("entry_order_ref") or ""),
                entry_broker_order_id=str(raw.get("entry_broker_order_id") or ""),
                entry_perm_id=int(raw["entry_perm_id"]) if raw.get("entry_perm_id") is not None else None,
                status=str(raw.get("status") or "intent_open"),
                last_exit_reason=str(raw.get("last_exit_reason") or ""),
                payload=dict(raw.get("payload") or {}),
            )
            if lot.position_lot_id:
                ledger._lots[lot.position_lot_id] = lot
        ledger._order_to_lot = {str(k): str(v) for k, v in dict(payload.get("order_to_lot") or {}).items()}
        ledger._broker_to_lot = {str(k): str(v) for k, v in dict(payload.get("broker_to_lot") or {}).items()}
        ledger._perm_to_lot = {
            int(k): str(v)
            for k, v in dict(payload.get("perm_to_lot") or {}).items()
            if str(k).strip()
        }
        ledger._conflicts = list(payload.get("conflicts") or [])
        return ledger

    def attach_order_identity(
        self,
        *,
        position_lot_id: str,
        order_ref: str | None = None,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
    ) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "attach_unknown_lot",
                    "position_lot_id": str(position_lot_id),
                    "order_ref": str(order_ref or ""),
                    "broker_order_id": str(broker_order_id or ""),
                    "perm_id": perm_id,
                }
            )
            return
        if order_ref:
            order_ref_s = str(order_ref)
            self._order_to_lot[order_ref_s] = str(position_lot_id)
            lot.entry_order_ref = order_ref_s
        if broker_order_id:
            broker_id_s = str(broker_order_id)
            self._broker_to_lot[broker_id_s] = str(position_lot_id)
            lot.entry_broker_order_id = broker_id_s
        if perm_id is not None:
            perm_id_i = int(perm_id)
            self._perm_to_lot[perm_id_i] = str(position_lot_id)
            lot.entry_perm_id = perm_id_i

    def lot_for_identity(
        self,
        *,
        order_ref: str | None = None,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
    ) -> Optional[ReplicaLotRecord]:
        lot_id: str | None = None
        if order_ref:
            lot_id = self._order_to_lot.get(str(order_ref))
        if lot_id is None and broker_order_id:
            lot_id = self._broker_to_lot.get(str(broker_order_id))
        if lot_id is None and perm_id is not None:
            lot_id = self._perm_to_lot.get(int(perm_id))
        if not lot_id:
            return None
        return self._lots.get(lot_id)

    def lot_by_id(self, position_lot_id: str | None) -> Optional[ReplicaLotRecord]:
        if not position_lot_id:
            return None
        return self._lots.get(str(position_lot_id))

    def mark_entry_live(self, *, position_lot_id: str, filled_qty: float) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "live_unknown_lot",
                    "position_lot_id": str(position_lot_id),
                    "filled_qty": float(filled_qty),
                }
            )
            return
        qty = max(0.0, float(filled_qty))
        lot.live_qty = qty
        lot.remaining_qty = qty
        lot.status = "live" if qty > 0 else "intent_open"

    def record_exit_fill(self, record: ReplicaExitFillRecord) -> None:
        self._exit_fills.append(record)
        lot = self._lots.get(record.position_lot_id)
        if lot is None:
            self._conflicts.append(
                {
                    "type": "exit_unknown_lot",
                    "position_lot_id": str(record.position_lot_id),
                    "qty": float(record.qty),
                    "reason": str(record.reason),
                }
            )
            return
        exit_qty = max(0.0, float(record.qty))
        lot.remaining_qty = max(0.0, float(lot.remaining_qty) - exit_qty)
        lot.live_qty = max(0.0, float(lot.live_qty) - exit_qty)
        lot.last_exit_reason = str(record.reason)
        if record.order_ref:
            self._order_to_lot[str(record.order_ref)] = record.position_lot_id
        if record.broker_order_id:
            self._broker_to_lot[str(record.broker_order_id)] = record.position_lot_id
        if record.perm_id is not None:
            self._perm_to_lot[int(record.perm_id)] = record.position_lot_id
        lot.status = "closed" if lot.remaining_qty <= 0.0 else "partial_exit"

    def allocate_exit_fill(
        self,
        *,
        strategy_id: str,
        signal_symbol: str,
        exec_symbol: str,
        qty: float,
        reason: str,
        ts: datetime,
        order_ref: str = "",
        broker_order_id: str = "",
        perm_id: int | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> float:
        remaining = max(0.0, float(qty))
        allocated = 0.0
        active_lots = self.active_lots(
            strategy_id=str(strategy_id),
            signal_symbol=str(signal_symbol),
            exec_symbol=str(exec_symbol),
        )
        for lot in active_lots:
            if remaining <= 0.0:
                break
            lot_qty = max(0.0, float(lot.remaining_qty))
            if lot_qty <= 0.0:
                continue
            fill_qty = min(lot_qty, remaining)
            self.record_exit_fill(
                ReplicaExitFillRecord(
                    strategy_id=str(strategy_id),
                    position_lot_id=lot.position_lot_id,
                    qty=fill_qty,
                    reason=str(reason),
                    ts=ts,
                    order_ref=order_ref,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    payload=dict(payload or {}),
                )
            )
            allocated += fill_qty
            remaining = max(0.0, remaining - fill_qty)
        if remaining > 0.0:
            self._conflicts.append(
                {
                    "type": "exit_unallocated_qty",
                    "strategy_id": str(strategy_id),
                    "signal_symbol": str(signal_symbol),
                    "exec_symbol": str(exec_symbol),
                    "requested_qty": float(qty),
                    "allocated_qty": allocated,
                    "remaining_qty": remaining,
                    "reason": str(reason),
                }
            )
        return allocated

    def sync_lot_live_qty(self, *, position_lot_id: str, live_qty: float) -> None:
        lot = self._lots.get(str(position_lot_id))
        if lot is None:
            self._conflicts.append(
                {
                    "type": "sync_unknown_lot",
                    "position_lot_id": str(position_lot_id),
                    "live_qty": float(live_qty),
                }
            )
            return
        qty = max(0.0, float(live_qty))
        lot.live_qty = qty
        lot.remaining_qty = qty
        lot.status = "closed" if qty <= 0.0 else "live"

    def close_active_lots(
        self,
        *,
        strategy_id: str,
        signal_symbol: Optional[str] = None,
        exec_symbol: Optional[str] = None,
        reason: str,
        ts: datetime,
    ) -> float:
        closed_qty = 0.0
        for lot in self.active_lots(strategy_id=strategy_id, signal_symbol=signal_symbol, exec_symbol=exec_symbol):
            if lot.remaining_qty <= 0.0:
                continue
            closed_qty += float(lot.remaining_qty)
            lot.live_qty = 0.0
            lot.remaining_qty = 0.0
            lot.last_exit_reason = str(reason)
            lot.status = "closed"
        return closed_qty

    def active_qty(
        self,
        *,
        strategy_id: Optional[str] = None,
        signal_symbol: Optional[str] = None,
        exec_symbol: Optional[str] = None,
    ) -> float:
        return sum(
            float(lot.remaining_qty)
            for lot in self.active_lots(
                strategy_id=strategy_id,
                signal_symbol=signal_symbol,
                exec_symbol=exec_symbol,
            )
        )

    def active_lots(
        self,
        *,
        strategy_id: Optional[str] = None,
        signal_symbol: Optional[str] = None,
        exec_symbol: Optional[str] = None,
    ) -> List[ReplicaLotRecord]:
        out: List[ReplicaLotRecord] = []
        for lot in self._lots.values():
            if strategy_id and lot.strategy_id != str(strategy_id):
                continue
            if signal_symbol and lot.signal_symbol != str(signal_symbol):
                continue
            if exec_symbol and lot.exec_symbol != str(exec_symbol):
                continue
            if float(lot.remaining_qty) <= 0.0:
                continue
            out.append(lot)
        return sorted(out, key=lambda x: (x.strategy_id, x.signal_symbol, x.exec_symbol, x.opened_at.isoformat(), x.position_lot_id))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "lots": [self._serialize_lot(lot) for lot in self._lots.values()],
            "order_to_lot": dict(self._order_to_lot),
            "broker_to_lot": dict(self._broker_to_lot),
            "perm_to_lot": dict(self._perm_to_lot),
            "intent_count": len(self._intents),
            "exit_fill_count": len(self._exit_fills),
            "conflicts": list(self._conflicts),
        }

    @staticmethod
    def _serialize_lot(lot: ReplicaLotRecord) -> Dict[str, Any]:
        payload = asdict(lot)
        if isinstance(lot.opened_at, datetime):
            payload["opened_at"] = lot.opened_at.isoformat()
        return payload
