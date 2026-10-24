import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.netting import NettingFillFanout


def test_netting_fill_fanout_allocates_and_releases(anyio_backend):
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        {
            "order_id": "00000000-0000-0000-0000-000000000011",
            "strategy_id": "s1",
            "decision_id": "d1",
            "idempotency_key": "idem1",
            "qty": 4.0,
            "state": "SUBMITTED",
            "created_at": None,
            "filled_qty": 0.0,
        },
        {
            "order_id": "00000000-0000-0000-0000-000000000022",
            "strategy_id": "s2",
            "decision_id": "d2",
            "idempotency_key": "idem2",
            "qty": 6.0,
            "state": "SUBMITTED",
            "created_at": None,
            "filled_qty": 0.0,
        },
    ])
    # allocated sum, child1 filled sum, child2 filled sum
    pg.fetchone = AsyncMock(side_effect=[
        {"allocated_qty": 0.0},
        {"filled_qty": 4.0},
        {"filled_qty": 2.0},
    ])
    pg.execute = AsyncMock(return_value="OK")

    reservation = MagicMock()
    reservation.release_on_terminal = AsyncMock(return_value=(True, 10.0))

    fanout = NettingFillFanout(pg=pg, reservation_service=reservation)
    fill = SimpleNamespace(ts_us=1_700_000_000_000_000, price=100.0, exec_id="exec-1", liquidity=None, exchange="SMART")

    __import__("asyncio").run(
        fanout.on_parent_fill(
            parent_order_uuid="00000000-0000-0000-0000-000000000001",
            parent_fill=fill,
            parent_filled_qty=6.0,
        )
    )

    assert pg.execute.await_count >= 4
    reservation.release_on_terminal.assert_awaited_once()
