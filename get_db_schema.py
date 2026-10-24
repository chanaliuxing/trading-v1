import asyncio
import json
from libs.db.postgres import PostgresDB

async def get_schema():
    pg = PostgresDB()
    query = """
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'core' AND table_name = 'core_order';
    """
    rows = await pg.fetchall(query)
    for row in rows:
        print(f"{row['column_name']}: {row['data_type']}")

if __name__ == "__main__":
    asyncio.run(get_schema())
