import asyncio
from libs.db.postgres import PostgresDB

async def find_hanging_orders():
    pg = PostgresDB()
    query = """
        SELECT order_id, broker_order_id, symbol, side, qty, state, created_at
        FROM core.core_order
        WHERE strategy_id = 'qqq_intraday_001'
          AND state NOT IN ('FILLED', 'CANCELLED', 'REJECTED');
    """
    rows = await pg.fetchall(query)
    for row in rows:
        print(f"Hanging Order: {dict(row)}")

if __name__ == "__main__":
    asyncio.run(find_hanging_orders())
