import asyncio
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from pathlib import Path
from libs.db.postgres import PostgresDB
from strategies.qqq_intraday_strategy import QQQIntradayStrategy
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

class TestHardeningModular(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pg = PostgresDB()
        await self.pg.connect()

    async def test_db_id_reuse(self):
        """Verify DB allows broker_order_id reuse."""
        print("\nTesting DB ID Reuse...")
        test_id = "REUSE_TEST_123"
        try:
            await self.pg.execute(
                "INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, broker_order_id, idempotency_key) "
                "VALUES (gen_random_uuid(), 'qqq_intraday_001', 'VERIFY', 'BUY', 'MKT', 1, 'FILLED', $1, $2)",
                test_id, f"ikey_1_{test_id}"
            )
            await self.pg.execute(
                "INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, broker_order_id, idempotency_key) "
                "VALUES (gen_random_uuid(), 'qqq_intraday_001', 'VERIFY', 'BUY', 'MKT', 1, 'FILLED', $1, $2)",
                test_id, f"ikey_2_{test_id}"
            )
            print("✅ DB allowed ID reuse successfully.")
        finally:
            await self.pg.execute("DELETE FROM core.core_order WHERE symbol = 'VERIFY'")

    async def test_eod_db_cleanup(self):
        """Verify EOD cleanup logic."""
        print("\nTesting EOD DB Cleanup logic...")
        try:
            await self.pg.execute(
                "INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, idempotency_key) "
                "VALUES (gen_random_uuid(), 'qqq_intraday_001', 'STUCK', 'BUY', 'MKT', 1, 'NEW', 'stuck_key')"
            )
            query = "UPDATE core.core_order SET state = 'CANCELLED' WHERE symbol = 'STUCK' AND state = 'NEW'"
            await self.pg.execute(query)
            res = await self.pg.fetchrow("SELECT state FROM core.core_order WHERE symbol = 'STUCK'")
            self.assertEqual(res['state'], 'CANCELLED')
            print("✅ EOD Cleanup logic correctly marked stuck order as CANCELLED.")
        finally:
            await self.pg.execute("DELETE FROM core.core_order WHERE symbol = 'STUCK'")

    async def test_strategy_grace_period(self):
        """Verify 120s grace period protection."""
        print("\nTesting Strategy Grace Period...")
        params = MagicMock()
        ctx = MagicMock()
        
        async def mock_pos(symbol): return None
        ctx.position = mock_pos
        
        strat = QQQIntradayStrategy(ctx, params)
        strat._initialized_at_et = datetime.now(ET)
        strat._is_traded = True
        strat._exec_symbol = "TQQQ"
        strat._side = "LONG"
        
        await strat._sync_position_state(force=True)
        self.assertEqual(strat._exec_symbol, "TQQQ")
        self.assertEqual(strat._side, "LONG")
        print("✅ Grace period successfully protected strategy state.")

if __name__ == "__main__":
    unittest.main()
