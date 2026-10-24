import asyncio
import json
from libs.db.postgres import PostgresDB

async def find_any_order_4():
    pg = PostgresDB()
    query = """
        SELECT order_id, strategy_id, symbol, side, qty, state, broker_order_id, note, created_at
        FROM core.core_order 
        WHERE broker_order_id = '4';
    """
    rows = await pg.fetchall(query)
    for row in rows:
        print(json.dumps(dict(row), indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(find_any_order_4())
