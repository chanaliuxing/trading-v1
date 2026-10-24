"""
EOD Cleanup Service
Runs after market close (e.g., 16:05 ET) to ensure a clean slate for the next day.
1. Cancels all open orders in IBKR.
2. Marks 'stuck' orders in DB as CANCELLED.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from libs.db.postgres import PostgresDB
from services.ibkr.ib_connection_manager import get_ib_connection_manager
from libs.logger import get_logger

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")

async def run_eod_cleanup():
    logger.info("🧹 STARTING EOD CLEANUP")
    
    # 1. IBKR Cleanup
    ib_mgr = get_ib_connection_manager()
    try:
        ib = await ib_mgr.get_exec_adapter(host='127.0.0.1', port=4002, account=None)
        
        # Sync orders
        logger.info("📡 Syncing IBKR open orders...")
        # Accessing the underlying IB instance from the adapter
        await ib.ib.reqAllOpenOrdersAsync()
        await asyncio.sleep(2)
        
        open_orders = ib.ib.openOrders()
        if open_orders:
            logger.warning(f"⚠️ Found {len(open_orders)} open orders in IBKR. Cancelling all...")
            for order in open_orders:
                ib.ib.cancelOrder(order)
            
            # Wait for cancellations to propagate
            await asyncio.sleep(3)
            logger.info("✅ IBKR orders cancelled.")
        else:
            logger.info("✅ IBKR is clean (no open orders).")
            
        await ib.stop()
        
    except Exception as e:
        logger.error(f"❌ IBKR Cleanup Failed: {e}")

    # 2. Database Cleanup
    pg = PostgresDB()
    try:
        # Mark stuck NEW/SUBMITTED orders as CANCELLED
        # Only target orders created in the last 24 hours to avoid touching historical archives if any
        query = """
            UPDATE core.core_order
            SET state = 'CANCELLED',
                note = COALESCE(note, '') || ' [EOD_AUTO_CLEANUP]',
                updated_at = now()
            WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL', 'PRESUBMITTED')
              AND created_at > now() - interval '24 hours'
            RETURNING order_id, symbol;
        """
        rows = await pg.fetch(query)
        if rows:
            logger.warning(f"🧹 Cleaned up {len(rows)} stuck orders in DB:", orders=[dict(r) for r in rows])
        else:
            logger.info("✅ Database is clean (no stuck orders).")
            
    except Exception as e:
        logger.error(f"❌ DB Cleanup Failed: {e}")
    finally:
        await pg.close()

    logger.info("🏁 EOD CLEANUP COMPLETE")

if __name__ == "__main__":
    asyncio.run(run_eod_cleanup())
