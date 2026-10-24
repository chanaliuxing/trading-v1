#!/usr/bin/env python3
"""
Check PostgreSQL tables for decision pipeline
"""
from libs.db.postgres import PostgresDB

def check_tables():
    """Check if all decision pipeline tables exist"""

    pg = PostgresDB()

    # Check if core schema exists
    tables = pg.fetchall("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'core'
        ORDER BY table_name
    """)

    print(f"✅ Found {len(tables)} tables in 'core' schema:")
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
        'core_risk_event',
        'shadow_orders'
    ]

    existing = [t['table_name'] for t in tables]
    missing = [t for t in key_tables if t not in existing]

    print(f"\n🔍 Key decision pipeline tables:")
    for table in key_tables:
        status = '✅' if table in existing else '❌ MISSING'
        print(f"   {status} {table}")

    if missing:
        print(f"\n⚠️  Missing {len(missing)} critical tables: {', '.join(missing)}")
        print("\n📝 To create missing tables, run:")
        print("   docker exec -i postgres psql -U postgres -d test < db/pg/pg_ddl.sql")
        print("\n   Or if PostgreSQL is running locally:")
        print("   /opt/homebrew/opt/postgresql@17/bin/psql -h localhost -U postgres -d test -f db/pg/pg_ddl.sql")
        return False
    else:
        print("\n✅ All decision pipeline tables exist!")
        return True


if __name__ == '__main__':
    try:
        success = check_tables()
        exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
