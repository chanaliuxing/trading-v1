import asyncio
from libs.db.postgres import PostgresDB

async def cleanup_dead_order():
    pg = PostgresDB()
    query = """
        UPDATE core.core_order
        SET state = 'CANCELLED',
            note = 'Manual cleanup of dead order with no broker_id',
            updated_at = now()
        WHERE strategy_id = 'qqq_intraday_001'
          AND state = 'NEW'
          AND broker_order_id IS NULL;
    """
    await pg.execute(query)
    print("✅ Dead order cleaned up from DB.")

if __name__ == "__main__":
    asyncio.run(cleanup_dead_order())
