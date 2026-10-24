"""
Regression tests for the fetchval → fetchone fix.

PostgresDB has no fetchval method. Both flatten_controller.probe_completion
and safety_kernel._clear_entry_lock_if_flat previously called it, causing
a silent AttributeError at runtime. These tests verify the corrected paths
use fetchone and extract COUNT(*) correctly.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from libs.db.postgres import PostgresDB
from services.exec.flatten_controller import FlattenController
from services.exec.safety_kernel import SafetyKernel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pg(*, pos_qty: float, open_count: int):
    """
    Mock using create_autospec so only methods that exist on PostgresDB can
    be called. Calling pg.fetchval() would raise AttributeError — exactly
    the contract we want to enforce.
    """
    pg = create_autospec(PostgresDB, instance=True)

    async def fetchone(query, *args):
        if "pos_qty" in query:
            return {"pos_qty": pos_qty}
        if "COUNT(*)" in query:
            return {"count": open_count}
        return None

    pg.fetchone = AsyncMock(side_effect=fetchone)
    return pg


# ---------------------------------------------------------------------------
# FlattenController.probe_completion
# ---------------------------------------------------------------------------

class TestFlattenControllerProbeCompletion:
    def _make_fc(self, *, pos_qty: float, open_count: int):
        return FlattenController(pg=_make_pg(pos_qty=pos_qty, open_count=open_count))

    def test_is_flat_when_no_position_and_no_orders(self):
        fc = self._make_fc(pos_qty=0.0, open_count=0)
        result = asyncio.get_event_loop().run_until_complete(
            fc.probe_completion(strategy_id="s1", symbol="SQQQ")
        )
        assert result["is_flat"] is True
        assert result["state"] == "confirmed"
        assert result["open_order_count"] == 0

    def test_pending_when_position_exists(self):
        fc = self._make_fc(pos_qty=88.0, open_count=0)
        result = asyncio.get_event_loop().run_until_complete(
            fc.probe_completion(strategy_id="s1", symbol="SQQQ")
        )
        assert result["is_flat"] is False
        assert result["state"] == "pending"

    def test_pending_when_open_orders_exist(self):
        fc = self._make_fc(pos_qty=0.0, open_count=1)
        result = asyncio.get_event_loop().run_until_complete(
            fc.probe_completion(strategy_id="s1", symbol="SQQQ")
        )
        assert result["is_flat"] is False
        assert result["open_order_count"] == 1

    def test_returns_pending_for_empty_args(self):
        fc = self._make_fc(pos_qty=0.0, open_count=0)
        result = asyncio.get_event_loop().run_until_complete(
            fc.probe_completion(strategy_id="", symbol="")
        )
        assert result["is_flat"] is False
        assert result["state"] == "pending"

    def test_fetchval_does_not_exist_on_postgres_db(self):
        """PostgresDB spec must not expose fetchval — that's the root cause of the bug."""
        assert not hasattr(PostgresDB, "fetchval")

    def test_fetchone_called_twice(self):
        pg = _make_pg(pos_qty=0.0, open_count=0)
        fc = FlattenController(pg=pg)
        asyncio.get_event_loop().run_until_complete(
            fc.probe_completion(strategy_id="s1", symbol="SQQQ")
        )
        assert pg.fetchone.call_count == 2


# ---------------------------------------------------------------------------
# SafetyKernel._clear_entry_lock_if_flat
# ---------------------------------------------------------------------------

class TestSafetyKernelClearEntryLockIfFlat:
    def _make_sk(self, *, pos_qty: float, open_count: int):
        pg = _make_pg(pos_qty=pos_qty, open_count=open_count)
        lock_store = MagicMock()
        lock_store.unlock = AsyncMock(return_value=True)
        sk = SafetyKernel.__new__(SafetyKernel)
        sk.pg = pg
        sk.manual_intervention_lock_store = lock_store
        return sk, lock_store

    def test_clears_lock_when_flat(self):
        sk, lock_store = self._make_sk(pos_qty=0.0, open_count=0)
        asyncio.get_event_loop().run_until_complete(
            sk._clear_entry_lock_if_flat(strategy_id="s1", symbol="SQQQ", reason="test")
        )
        lock_store.unlock.assert_called_once()

    def test_does_not_clear_when_position_exists(self):
        sk, lock_store = self._make_sk(pos_qty=44.0, open_count=0)
        asyncio.get_event_loop().run_until_complete(
            sk._clear_entry_lock_if_flat(strategy_id="s1", symbol="SQQQ", reason="test")
        )
        lock_store.clear.assert_not_called()

    def test_does_not_clear_when_open_orders(self):
        sk, lock_store = self._make_sk(pos_qty=0.0, open_count=2)
        asyncio.get_event_loop().run_until_complete(
            sk._clear_entry_lock_if_flat(strategy_id="s1", symbol="SQQQ", reason="test")
        )
        lock_store.clear.assert_not_called()

    def test_fetchone_called_twice_when_flat(self):
        sk, _ = self._make_sk(pos_qty=0.0, open_count=0)
        asyncio.get_event_loop().run_until_complete(
            sk._clear_entry_lock_if_flat(strategy_id="s1", symbol="SQQQ", reason="test")
        )
        assert sk.pg.fetchone.call_count == 2

    def test_skips_gracefully_when_pg_none(self):
        sk = SafetyKernel.__new__(SafetyKernel)
        sk.pg = None
        sk.manual_intervention_lock_store = None
        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            sk._clear_entry_lock_if_flat(strategy_id="s1", symbol="SQQQ", reason="test")
        )
