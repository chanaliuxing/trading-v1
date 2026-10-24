import asyncio
from libs.db.postgres import PostgresDB

async def check_strategy_schema():
    pg = PostgresDB()
    rows = await pg.fetchall("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_schema = 'core' AND table_name = 'core_strategy'
    """)
    print([r['column_name'] for r in rows])

if __name__ == "__main__":
    asyncio.run(check_strategy_schema())
