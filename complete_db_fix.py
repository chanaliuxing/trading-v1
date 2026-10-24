import asyncio
from libs.db.postgres import PostgresDB

async def complete_db_hardening():
    pg = PostgresDB()
    # 1. Identify and drop ALL unique constraints on broker_order_id
    # This is more robust than just ALTER TABLE DROP CONSTRAINT
    sql = """
    DO $$ 
    DECLARE 
        r RECORD;
    BEGIN
        FOR r IN (SELECT constraint_name 
                  FROM information_schema.constraint_column_usage 
                  WHERE table_name = 'core_order' AND column_name = 'broker_order_id') 
        LOOP
            EXECUTE 'ALTER TABLE core.core_order DROP CONSTRAINT IF EXISTS ' || quote_ident(r.constraint_name) || ' CASCADE';
        END LOOP;
    END $$;
    """
    await pg.execute(sql)
    
    # 2. Also drop any unique indexes that aren't formal constraints
    await pg.execute("DROP INDEX IF EXISTS core.core_order_broker_id_uk")
    await pg.execute("DROP INDEX IF EXISTS core.idx_core_order_broker_id_unique")
    
    # 3. Create a non-unique index for performance
    await pg.execute("CREATE INDEX IF NOT EXISTS idx_core_order_broker_id_non_unique ON core.core_order (broker_order_id)")
    
    print("✅ Full Database Hardening Complete: All unique restrictions on broker_order_id removed.")

if __name__ == "__main__":
    asyncio.run(complete_db_hardening())
