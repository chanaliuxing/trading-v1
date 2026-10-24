from unittest.mock import AsyncMock, MagicMock

import pytest

from services.portfolio.account_service import AccountService


@pytest.mark.asyncio
async def test_shutdown_does_not_stop_shared_exec_adapter():
    svc = AccountService()
    adapter = MagicMock()
    adapter.stop = AsyncMock()
    svc._adapter = adapter

    await svc.shutdown()

    adapter.stop.assert_not_called()
    assert svc._adapter is None
