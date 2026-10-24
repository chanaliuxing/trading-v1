import asyncio
import time
import uuid
from libs.db.postgres import PostgresDB

async def test_db_stability():
    pg = PostgresDB()
    print("🚀 Starting DB stability test with idempotency_key...")
    for i in range(1, 11):
        try:
            start = time.time()
            ikey = f"test_key_{uuid.uuid4()}"
            await pg.execute("INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, idempotency_key) VALUES (gen_random_uuid(), 'test', 'TEST', 'BUY', 'MKT', 1, 'CANCELLED', $1)", ikey)
            await pg.execute("DELETE FROM core.core_order WHERE strategy_id = 'test'")
            end = time.time()
            print(f"✅ Pass {i}: {(end-start)*1000:.2f}ms")
        except Exception as e:
            print(f"❌ Fail {i}: {e}")
    print("🏁 Test complete.")

if __name__ == "__main__":
    asyncio.run(test_db_stability())
