import asyncio
from libs.db.postgres import PostgresDB

async def verify_constraints():
    pg = PostgresDB()
    query = """
        SELECT conname, contype 
        FROM pg_constraint 
        WHERE conrelid = 'core.core_order'::regclass;
    """
    rows = await pg.fetchall(query)
    print("🛡️ Current Database Constraints on core_order:")
    for r in rows:
        print(f"   - {r['conname']} (Type: {r['contype']})")

if __name__ == "__main__":
    asyncio.run(verify_constraints())
