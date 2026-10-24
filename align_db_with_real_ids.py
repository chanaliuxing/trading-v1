import asyncio
from libs.db.postgres import PostgresDB

async def fix_database_ids():
    pg = PostgresDB()
    # Update with REAL IDs from IBKR
    query = """
        UPDATE core.core_order 
        SET broker_order_id = '4',
            ib_perm_id = 1632726515,
            state = 'FILLED',
            note = 'Database aligned with real IBKR IDs after logic gap recovery'
        WHERE order_id = 'e8463583-d1a0-4fe5-be81-cbec6778fe5a';
    """
    await pg.execute(query)
    print("✅ Database updated with REAL Broker IDs (OrderId=4, PermId=1632726515).")

if __name__ == "__main__":
    asyncio.run(fix_database_ids())
