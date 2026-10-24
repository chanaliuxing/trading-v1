#!/usr/bin/env python3
"""
Apply PostgreSQL schema to test database
Run this to ensure all tables for decision pipeline exist
"""
import asyncio
import asyncpg


async def apply_schema():
    """Apply schema from pg_ddl.sql file"""

    # Read schema file
    with open('db/pg/pg_ddl.sql', 'r') as f:
        schema_sql = f.read()

    # Connect to database
    conn = await asyncpg.connect(
        host='localhost',
        port=5432,
        user='postgres',
        password='postgres',
        database='test'
    )

    print("✅ Connected to PostgreSQL database 'test'")

    try:
        # Check if core schema exists
        result = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'core')"
        )

        if result:
            print("✅ Schema 'core' exists")

            # List existing tables
            tables = await conn.fetch("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'core'
                ORDER BY table_name
            """)

            print(f"\n📊 Found {len(tables)} tables in 'core' schema:")
            for t in tables:
                print(f"   - {t['table_name']}")

            # Check key decision pipeline tables
            key_tables = [
                'core_order',
                'core_order_reservation',
                'core_strategy',
                'core_strategy_ledger',
                'core_position',
                'core_fill',
                'core_risk_event'
            ]

            existing = [t['table_name'] for t in tables]
            missing = [t for t in key_tables if t not in existing]

            print(f"\n🔍 Key decision pipeline tables:")
            for table in key_tables:
                status = '✅' if table in existing else '❌ MISSING'
                print(f"   {status} {table}")

            if missing:
                print(f"\n⚠️  Missing {len(missing)} critical tables!")
                print("\nYou can apply the full schema by running:")
                print("   cd .")
                print("   psql -h localhost -U postgres -d test -f db/pg/pg_ddl.sql")
                return False
            else:
                print("\n✅ All decision pipeline tables exist!")
                return True

        else:
            print("❌ Schema 'core' does not exist!")
            print("\nApplying schema from db/pg/pg_ddl.sql...")

            # Apply schema
            await conn.execute(schema_sql)
            print("✅ Schema applied successfully!")
            return True

    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        success = asyncio.run(apply_schema())
        exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        exit(1)
