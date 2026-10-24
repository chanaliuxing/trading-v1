import asyncio
import unittest
import uuid
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock
from zoneinfo import ZoneInfo
from libs.db.postgres import PostgresDB
from services.reconciliation.runtime_reconciler import RuntimeReconciler
from strategies.qqq_intraday_strategy import QQQIntradayStrategy

ET = ZoneInfo("America/New_York")

class SystemIntegrityStressTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pg = PostgresDB()
        await self.pg.connect()
        self.test_run_id = f"st_{int(time.time())}"

    async def asyncTearDown(self):
        try:
            await self.pg.execute("DELETE FROM core.core_order WHERE symbol LIKE 'STRESS_%'")
        except: pass
        await self.pg.close()

    async def test_case_1_id_collision_stress(self):
        """Stress Test 1: Broker ID Reuse (Constraint Relaxation)."""
        print("\n🔥 Test 1: Broker ID Reuse...")
        shared_broker_id = f"COL_{int(time.time())}"
        success_count = 0
        for i in range(5):
            try:
                ikey = f"ikey_{self.test_run_id}_{i}"
                sym = f"STRESS_SYM_{i}"
                # FIXED: 9 columns, 9 values
                await self.pg.execute("""
                    INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, broker_order_id, idempotency_key)
                    VALUES (gen_random_uuid(), 'qqq_intraday_001', $1, 'BUY', 'MKT', 1, 'FILLED', $2, $3)
                """, sym, shared_broker_id, ikey)
                success_count += 1
            except Exception as e:
                print(f"   ❌ Failed at {i}: {e}")
        
        self.assertEqual(success_count, 5)
        print(f"   ✅ Pass: DB handled {success_count} collisions successfully.")

    async def test_case_2_position_inversion_detection(self):
        """Stress Test 2: Position Inversion Detection."""
        print("\n🔥 Test 2: Position Inversion Detection...")
        ikey = f"ik_inv_{self.test_run_id}"
        await self.pg.execute("""
            INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, idempotency_key)
            VALUES (gen_random_uuid(), 'qqq_intraday_001', 'STRESS_SQQQ', 'BUY', 'MKT', 100, 'FILLED', $1)
        """, ikey)

        mock_ib_pos = MagicMock()
        mock_ib_pos.contract.symbol = "STRESS_SQQQ"
        mock_ib_pos.position = -100.0 # INVERTED
        
        adapter = MagicMock()
        adapter.positions = AsyncMock(return_value=[mock_ib_pos])
        adapter.open_orders = AsyncMock(return_value=[])
        
        reconciler = RuntimeReconciler(self.pg, adapter)
        
        with self.assertLogs('services.reconciliation.runtime_reconciler', level='ERROR') as cm:
            await reconciler.run_once()
            has_alarm = any("POSITION_MISMATCH_DETECTED" in output for output in cm.output)
            self.assertTrue(has_alarm)
            print("   ✅ Pass: Auditor correctly identified mirrored mismatch.")

    async def test_case_3_grace_period_flapping(self):
        """Stress Test 3: Grace Period Protection."""
        print("\n🔥 Test 3: Startup Grace Period...")
        ctx = MagicMock()
        async def mock_pos(symbol): return None
        ctx.position = mock_pos
        
        strat = QQQIntradayStrategy(ctx, MagicMock())
        strat._initialized_at_et = datetime.now(ET)
        strat._is_traded = True
        strat._exec_symbol = "TQQQ"
        strat._side = "LONG"
        
        # Still in grace period
        await strat._sync_position_state(force=True)
        self.assertEqual(strat._exec_symbol, "TQQQ")

        # After grace period
        strat._initialized_at_et = datetime.now(ET) - timedelta(seconds=130)
        await strat._sync_position_state(force=True)
        self.assertIsNone(strat._exec_symbol)
        print("   ✅ Pass: Grace period correctly protected persistent state.")

    async def test_case_4_bulk_adoption(self):
        """Stress Test 4: Bulk Orphan Adoption FK Check."""
        print("\n🔥 Test 4: Bulk Orphan Adoption...")
        ikey_prefix = f"ORPH_{self.test_run_id}"
        orphans = []
        for i in range(3):
            o = MagicMock()
            o.order_id = f"{ikey_prefix}_{i}"
            o.perm_id = 2000 + i
            o.symbol = "STRESS_ORPH"
            o.side = "SELL"
            o.qty = 10.0
            o.status = "Submitted"
            o.order_type = "LMT"
            orphans.append(o)
            
        adapter = MagicMock()
        adapter.open_orders = AsyncMock(return_value=orphans)
        adapter.positions = AsyncMock(return_value=[])
        
        reconciler = RuntimeReconciler(self.pg, adapter)
        await reconciler.run_once()
        
        # FIXED: Correct way to access count from row object
        row = await self.pg.fetchrow("SELECT count(*) as cnt FROM core.core_order WHERE strategy_id = 'adopted_reconciler' AND symbol = 'STRESS_ORPH'")
        self.assertGreaterEqual(row['cnt'], 3)
        print(f"   ✅ Pass: Successfully adopted {row['cnt']} bulk orphan orders.")

if __name__ == "__main__":
    unittest.main()
