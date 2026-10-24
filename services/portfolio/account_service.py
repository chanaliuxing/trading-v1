"""
Account service for querying IBKR account summary/available funds.

Provides a singleton-style helper so strategies and other components can
request account metrics without manually instantiating IB adapters.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from libs.config import config as CFG
from libs.logger import get_logger
from services.exec.adapters.ib_adapter import IBInsyncAdapter
from services.ibkr.ib_connection_manager import get_ib_connection_manager

logger = get_logger(__name__)


class AccountService:
    """Lazy-loading IB account service."""

    def __init__(self):
        self._adapter: Optional[IBInsyncAdapter] = None
        self._adapter_epoch: Optional[int] = None
        self._lock = asyncio.Lock()

    async def _ensure_adapter(self) -> None:
        # Prefer shared execution adapter to avoid extra IB sessions.
        try:
            ib_cfg = CFG.ib_config
            host = ib_cfg.get("host", "127.0.0.1")
            port = int(ib_cfg.get("port", 4002))
            account = os.getenv("IB_ACCOUNT", "")
            ibm = get_ib_connection_manager()
            current_epoch = int(ibm.exec_connection_epoch())
            if self._adapter is not None and self._adapter_epoch is not None and self._adapter_epoch == current_epoch:
                return
            self._adapter = await ibm.get_exec_adapter(host=host, port=port, account=account)
            self._adapter_epoch = current_epoch
            return
        except Exception as exc:
            logger.warning("account_service_shared_adapter_unavailable", error=str(exc))
            raise

    async def available_funds(self) -> Optional[float]:
        """Return IB AvailableFunds (USD) if accessible."""
        try:
            await self._ensure_adapter()
            return await self._adapter.available_funds() if self._adapter else None
        except Exception:
            return None

    async def shutdown(self) -> None:
        """Release local reference to the shared adapter without stopping it."""
        self._adapter = None
        self._adapter_epoch = None


_ACCOUNT_SERVICE = AccountService()


def get_account_service() -> AccountService:
    """Return shared account service instance."""
    return _ACCOUNT_SERVICE
