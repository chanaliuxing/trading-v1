import asyncio
import json
from libs.db.postgres import PostgresDB

async def get_order_audit():
    pg = PostgresDB()
    query = """
        SELECT order_id, broker_order_id, state, created_at, submitted_at, updated_at, note
        FROM core.core_order 
        WHERE order_id = 'e8463583-d1a0-4fe5-be81-cbec6778fe5a';
    """
    rows = await pg.fetchall(query)
    for row in rows:
        print(json.dumps(dict(row), indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(get_order_audit())
