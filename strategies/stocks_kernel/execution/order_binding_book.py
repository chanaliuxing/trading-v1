from __future__ import annotations

from dataclasses import replace
from typing import Optional

from strategies.stocks_kernel.types import LotOrderBinding, LotProtectionChainView


class LotOrderBindingBook:
    """
    Lot-scoped order binding registry.

    Each order binding is attached to one position_lot_id. Resolution is exact;
    ambiguous or duplicate identity registration is rejected at write time.
    """

    def __init__(self) -> None:
        self._bindings_by_ref: dict[str, LotOrderBinding] = {}
        self._order_ref_by_broker_id: dict[str, str] = {}
        self._order_ref_by_perm_id: dict[int, str] = {}

    def bind(self, binding: LotOrderBinding) -> LotOrderBinding:
        order_ref = str(binding.order_ref or "").strip()
        if not order_ref:
            raise ValueError("order_ref is required")
        broker_order_id = str(binding.broker_order_id or "").strip() or None
        perm_id = int(binding.perm_id) if binding.perm_id is not None else None
        normalized = replace(
            binding,
            symbol=str(binding.symbol or "").strip().upper(),
            order_role=str(binding.order_role or "").strip().upper(),
            order_ref=order_ref,
            broker_order_id=broker_order_id,
            perm_id=perm_id,
        )

        existing = self._bindings_by_ref.get(order_ref)
        if existing is not None and existing != normalized:
            raise ValueError(f"order_ref already bound: {order_ref}")

        if broker_order_id:
            existing_ref = self._order_ref_by_broker_id.get(broker_order_id)
            if existing_ref is not None and existing_ref != order_ref:
                raise ValueError(f"broker_order_id already bound: {broker_order_id}")
        if perm_id is not None:
            existing_ref = self._order_ref_by_perm_id.get(perm_id)
            if existing_ref is not None and existing_ref != order_ref:
                raise ValueError(f"perm_id already bound: {perm_id}")

        self._bindings_by_ref[order_ref] = normalized
        if broker_order_id:
            self._order_ref_by_broker_id[broker_order_id] = order_ref
        if perm_id is not None:
            self._order_ref_by_perm_id[perm_id] = order_ref
        return normalized

    def resolve(
        self,
        *,
        order_ref: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> Optional[LotOrderBinding]:
        if perm_id is not None:
            ref = self._order_ref_by_perm_id.get(int(perm_id))
            if ref:
                return replace(self._bindings_by_ref[ref])
        broker_order_id_v = str(broker_order_id or "").strip()
        if broker_order_id_v:
            ref = self._order_ref_by_broker_id.get(broker_order_id_v)
            if ref:
                return replace(self._bindings_by_ref[ref])
        order_ref_v = str(order_ref or "").strip()
        if order_ref_v and order_ref_v in self._bindings_by_ref:
            return replace(self._bindings_by_ref[order_ref_v])
        return None

    def attach_broker_identity(
        self,
        *,
        order_ref: str,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> LotOrderBinding:
        order_ref_v = str(order_ref or "").strip()
        existing = self._bindings_by_ref.get(order_ref_v)
        if existing is None:
            raise KeyError(f"unknown order_ref: {order_ref_v}")
        broker_order_id_v = str(broker_order_id or "").strip() or None
        perm_id_v = int(perm_id) if perm_id is not None else None
        if broker_order_id_v:
            other_ref = self._order_ref_by_broker_id.get(broker_order_id_v)
            if other_ref is not None and other_ref != order_ref_v:
                raise ValueError(f"broker_order_id already bound: {broker_order_id_v}")
        if perm_id_v is not None:
            other_ref = self._order_ref_by_perm_id.get(perm_id_v)
            if other_ref is not None and other_ref != order_ref_v:
                raise ValueError(f"perm_id already bound: {perm_id_v}")
        updated = replace(
            existing,
            broker_order_id=broker_order_id_v or existing.broker_order_id,
            perm_id=perm_id_v if perm_id_v is not None else existing.perm_id,
        )
        self._bindings_by_ref[order_ref_v] = updated
        if updated.broker_order_id:
            self._order_ref_by_broker_id[updated.broker_order_id] = order_ref_v
        if updated.perm_id is not None:
            self._order_ref_by_perm_id[updated.perm_id] = order_ref_v
        return replace(updated)

    def protection_chain(self, *, position_lot_id: str) -> LotProtectionChainView:
        lot_id = str(position_lot_id or "").strip()
        entry_order_ref: Optional[str] = None
        stop_order_refs: list[str] = []
        flatten_order_refs: list[str] = []
        strategy_id = ""
        symbol = ""
        for binding in self._bindings_by_ref.values():
            if binding.position_lot_id != lot_id:
                continue
            strategy_id = binding.strategy_id
            symbol = binding.symbol
            if binding.order_role == "ENTRY":
                entry_order_ref = binding.order_ref
            elif binding.order_role == "STOP":
                stop_order_refs.append(binding.order_ref)
            elif binding.order_role == "FLATTEN":
                flatten_order_refs.append(binding.order_ref)
        return LotProtectionChainView(
            position_lot_id=lot_id,
            strategy_id=strategy_id,
            symbol=symbol,
            entry_order_ref=entry_order_ref,
            stop_order_refs=tuple(sorted(stop_order_refs)),
            flatten_order_refs=tuple(sorted(flatten_order_refs)),
        )
