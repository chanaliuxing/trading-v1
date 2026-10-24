import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.netting import ChildOrderState, FillAllocator


def test_fill_allocator_allocates_fifo_and_updates_remaining_qty():
    allocator = FillAllocator()
    children = [
        ChildOrderState(child_order_id="c1", strategy_id="s1", decision_id="d1", remaining_qty=4),
        ChildOrderState(child_order_id="c2", strategy_id="s2", decision_id="d2", remaining_qty=7),
    ]

    allocations, left = allocator.allocate(fill_qty=6, fill_price=100.5, children=children)

    assert left == 0
    assert len(allocations) == 2
    assert allocations[0].child_order_id == "c1"
    assert allocations[0].qty == 4
    assert allocations[1].child_order_id == "c2"
    assert allocations[1].qty == 2
    assert children[0].remaining_qty == 0
    assert children[1].remaining_qty == 5


def test_fill_allocator_returns_unallocated_when_parent_fill_exceeds_child_total():
    allocator = FillAllocator()
    children = [
        ChildOrderState(child_order_id="c1", strategy_id="s1", decision_id="d1", remaining_qty=1.5),
    ]

    allocations, left = allocator.allocate(fill_qty=3.0, fill_price=50.0, children=children)

    assert len(allocations) == 1
    assert allocations[0].qty == 1.5
    assert left == 1.5
    assert children[0].remaining_qty == 0
