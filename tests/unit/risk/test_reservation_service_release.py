from unittest.mock import AsyncMock, MagicMock

import pytest

from services.risk.reservation_service import ReservationService


@pytest.mark.asyncio
async def test_release_uses_valid_zero_uuid_for_order_id():
    db = MagicMock()
    db.fetchone = AsyncMock(return_value={"released": True, "released_usd": 0})
    svc = ReservationService(db=db)

    ok = await svc.release("idem-1")
    assert ok is True

    assert db.fetchone.await_count == 1
    args = db.fetchone.await_args.args
    assert args[0].startswith("SELECT * FROM core_sp_release_on_terminal")
    assert args[1] == "idem-1"
    assert args[2] == "00000000-0000-0000-0000-000000000000"
    assert args[3] == "CANCELLED"
