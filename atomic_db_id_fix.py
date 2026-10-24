import asyncio
from libs.db.postgres import PostgresDB

async def atomic_fix():
    pg = PostgresDB()
    # 1. Rename old broker_id to free up '4'
    await pg.execute("UPDATE core.core_order SET broker_order_id = 'HIST_4' WHERE broker_order_id = '4' AND symbol = 'SQQQ'")
    
    # 2. Assign '4' to today's TQQQ order
    await pg.execute("""
        UPDATE core.core_order 
        SET broker_order_id = '4',
            ib_perm_id = 1632726515,
            state = 'FILLED',
            note = 'Restored today from orphan state'
        WHERE order_id = 'e8463583-d1a0-4fe5-be81-cbec6778fe5a'
    """)
    print("✅ Database IDs swapped. OrderId=4 now correctly points to today's TQQQ.")

if __name__ == "__main__":
    asyncio.run(atomic_fix())
