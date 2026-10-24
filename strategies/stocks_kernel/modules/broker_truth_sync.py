from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from strategies.stocks_kernel.execution.order_identity import parse_protection_order_ref


@dataclass(frozen=True)
class BrokerTruthSyncReport:
    ok: bool
    reason: str
    orphan_order_refs: tuple[str, ...]
    missing_order_refs: tuple[str, ...]
    freeze_symbols: tuple[str, ...]


class BrokerTruthSyncModule:
    """
    Compare runtime kernel order view against broker open-order truth.
    """

    def __init__(self) -> None:
        self._missing_seen_count: dict[str, int] = {}

    def evaluate(
        self,
        *,
        strategy_id: str,
        kernel,
        broker_open_orders: tuple[Any, ...],
        tracked_symbols: tuple[str, ...],
    ) -> BrokerTruthSyncReport:
        strategy_id_v = str(strategy_id or "").strip()
        tracked = {str(s or "").strip().upper() for s in tracked_symbols if str(s or "").strip()}
        broker_refs: set[str] = set()
        orphan_refs: list[str] = []
        freeze_symbols: set[str] = set()
        protected_owner_symbols: set[str] = set()
        for order in broker_open_orders:
            order_ref = str(getattr(order, "order_ref", "") or "").strip()
            symbol = str(getattr(order, "symbol", "") or "").strip().upper()
            if not order_ref:
                continue
            protection_ref = parse_protection_order_ref(order_ref)
            protection_symbol = str(getattr(protection_ref, "symbol", "") or "").strip().upper() if protection_ref else ""
            effective_symbol = symbol or protection_symbol
            if tracked and effective_symbol and effective_symbol not in tracked:
                continue
            if protection_ref is not None and symbol and protection_symbol and protection_symbol != symbol:
                orphan_refs.append(order_ref)
                freeze_symbols.add(symbol)
                continue
            protection_owner_strategy = str(getattr(protection_ref, "strategy_id", "") or "").strip() if protection_ref else ""
            if protection_owner_strategy and protection_owner_strategy != strategy_id_v:
                continue
            if protection_ref is not None and protection_owner_strategy == strategy_id_v and effective_symbol:
                protected_owner_symbols.add(effective_symbol)
            broker_refs.add(order_ref)
            binding = kernel.bindings.resolve(
                order_ref=order_ref or None,
                broker_order_id=str(getattr(order, "broker_order_id", "") or "").strip() or None,
                perm_id=getattr(order, "perm_id", None),
            )
            if binding is None:
                if protection_owner_strategy == strategy_id_v:
                    continue
                orphan_refs.append(order_ref)
                if effective_symbol:
                    freeze_symbols.add(effective_symbol)
                continue
            if str(getattr(binding, "strategy_id", "") or "").strip() != strategy_id_v:
                if effective_symbol:
                    freeze_symbols.add(effective_symbol)

        missing_refs: list[str] = []
        active_states = {"pending_entry", "pending_exit"}
        for lot in kernel.ledger.list_lots(strategy_id=strategy_id_v, include_closed=False):
            if tracked and lot.symbol not in tracked:
                continue
            if str(lot.state or "").strip().lower() not in active_states:
                continue
            ref = str(lot.entry_order_ref or "").strip()
            if not ref:
                continue
            if ref in broker_refs:
                self._missing_seen_count.pop(ref, None)
                continue
            if lot.symbol in protected_owner_symbols:
                self._missing_seen_count.pop(ref, None)
                continue
            seen = int(self._missing_seen_count.get(ref) or 0) + 1
            self._missing_seen_count[ref] = seen
            # Two consecutive probes before escalation to reduce one-shot snapshot noise.
            if seen >= 2:
                missing_refs.append(ref)
                freeze_symbols.add(lot.symbol)

        ok = not orphan_refs and not missing_refs
        reason = "ready" if ok else "broker_truth_mismatch"
        return BrokerTruthSyncReport(
            ok=ok,
            reason=reason,
            orphan_order_refs=tuple(sorted(set(orphan_refs))),
            missing_order_refs=tuple(sorted(set(missing_refs))),
            freeze_symbols=tuple(sorted(freeze_symbols)),
        )
