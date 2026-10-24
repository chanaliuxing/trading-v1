from unittest.mock import AsyncMock, MagicMock

import pytest

from services.reconciliation.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_runtime_reconciler_acknowledge_order_quarantine_resolves_active_row():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="1")
    rec = RuntimeReconciler(pg=pg, ib_adapter=MagicMock())

    updated = await rec.acknowledge_order_quarantine(
        actor="ops",
        symbol="QQQ",
        broker_order_id="1234",
        note="verified and cleared",
        resolve=True,
    )

    assert updated == 1
    sql = pg.execute.await_args.args[0]
    assert "UPDATE core.core_order_quarantine" in sql
