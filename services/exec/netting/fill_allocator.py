from __future__ import annotations

from typing import Iterable, List, Tuple

from .models import ChildOrderState, FillAllocation


class FillAllocator:
    """Deterministic FIFO allocator from parent fills to child orders."""

    def allocate(
        self,
        *,
        fill_qty: float,
        fill_price: float,
        children: Iterable[ChildOrderState],
    ) -> Tuple[List[FillAllocation], float]:
        qty_left = float(fill_qty)
        price = float(fill_price)
        if qty_left <= 0:
            raise ValueError("fill_qty must be > 0")
        if price <= 0:
            raise ValueError("fill_price must be > 0")

        allocations: List[FillAllocation] = []
        for child in children:
            if qty_left <= 0:
                break

            remaining = float(child.remaining_qty)
            if remaining <= 0:
                continue

            take = min(remaining, qty_left)
            if take <= 0:
                continue

            child.remaining_qty = remaining - take
            qty_left -= take
            allocations.append(
                FillAllocation(
                    child_order_id=child.child_order_id,
                    strategy_id=child.strategy_id,
                    decision_id=child.decision_id,
                    qty=take,
                    price=price,
                )
            )

        return allocations, qty_left
