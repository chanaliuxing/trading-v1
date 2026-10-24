#!/usr/bin/env python3
"""
PostgreSQL Database Backup Script

Creates a full backup of the trading database including:
- Schema structure (CREATE TABLE statements)
- Constraints and indexes
- All data (INSERT statements)

Usage:
    python scripts/backup_postgres.py

Output:
    backups/postgresql/trading_full_backup_YYYYMMDD_HHMMSS.sql
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.config import config
import psycopg2
from datetime import datetime


def backup_database():
    """Create full database backup"""

    # Create backup directory
    backup_dir = "backups/postgresql"
    os.makedirs(backup_dir, exist_ok=True)

    # Connect to database
    print(f"Connecting to database '{config.pg_config['database']}'...")
    conn = psycopg2.connect(
        host=config.pg_config['host'],
        port=config.pg_config['port'],
        database=config.pg_config['database'],
        user=config.pg_config['user'],
        password=config.pg_config['password']
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"{backup_dir}/trading_full_backup_{timestamp}.sql"

    print(f"Creating backup: {backup_file}")

    with open(backup_file, 'w') as f:
        cur = conn.cursor()

        # Write header
        f.write(f"-- ============================================\n")
        f.write(f"-- PostgreSQL Database Full Backup\n")
        f.write(f"-- ============================================\n")
        f.write(f"-- Database: {config.pg_config['database']}\n")
        f.write(f"-- Timestamp: {datetime.now()}\n")
        f.write(f"-- Host: {config.pg_config['host']}\n")
        f.write(f"-- ============================================\n\n")

        # Create core schema
        f.write("CREATE SCHEMA IF NOT EXISTS core;\n\n")

        # Get all tables
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'core'
            ORDER BY tablename
        """)
        tables = [row[0] for row in cur.fetchall()]

        print(f"Found {len(tables)} tables in 'core' schema")

        total_rows = 0

        # Backup each table
        for i, table in enumerate(tables, 1):
            print(f"  [{i}/{len(tables)}] Backing up core.{table}...", end=' ')

            f.write(f"\n-- =============================================\n")
            f.write(f"-- Table: core.{table}\n")
            f.write(f"-- =============================================\n\n")

            # Drop table
            f.write(f"DROP TABLE IF EXISTS core.{table} CASCADE;\n\n")

            # Get table structure
            cur.execute(f"""
                SELECT column_name, data_type, character_maximum_length,
                       is_nullable, column_default, udt_name
                FROM information_schema.columns
                WHERE table_schema = 'core' AND table_name = '{table}'
                ORDER BY ordinal_position
            """)

            columns = []
            for col in cur.fetchall():
                col_name, data_type, max_len, nullable, default, udt_name = col

                if data_type == 'ARRAY':
                    col_def = f"    {col_name} {udt_name}[]"
                elif data_type == 'USER-DEFINED':
                    col_def = f"    {col_name} {udt_name}"
                else:
                    col_def = f"    {col_name} {data_type}"
                    if max_len and data_type in ('character varying', 'character'):
                        col_def += f"({max_len})"

                if default:
                    col_def += f" DEFAULT {default}"
                if nullable == 'NO':
                    col_def += " NOT NULL"
                columns.append(col_def)

            f.write(f"CREATE TABLE core.{table} (\n")
            f.write(",\n".join(columns))
            f.write("\n);\n\n")

            # Get constraints
            cur.execute(f"""
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'core.{table}'::regclass
                ORDER BY conname
            """)
            constraints = cur.fetchall()
            for con_name, con_def in constraints:
                f.write(f"ALTER TABLE core.{table} ADD CONSTRAINT {con_name} {con_def};\n")

            if constraints:
                f.write("\n")

            # Get indexes
            cur.execute(f"""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'core' AND tablename = '{table}'
                AND indexname NOT LIKE '%_pkey'
            """)
            indexes = cur.fetchall()
            for idx_name, idx_def in indexes:
                f.write(f"{idx_def};\n")

            if indexes:
                f.write("\n")

            # Get row count
            cur.execute(f'SELECT COUNT(*) FROM core."{table}"')
            row_count = cur.fetchone()[0]
            f.write(f"-- Row count: {row_count:,}\n")

            # Insert data if exists
            if row_count > 0:
                f.write(f"\n-- Data for core.{table}\n")

                # Get column names
                cur.execute(f"""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'core' AND table_name = '{table}'
                    ORDER BY ordinal_position
                """)
                col_names = [row[0] for row in cur.fetchall()]

                # Get all data
                cur.execute(f'SELECT * FROM core."{table}"')
                rows = cur.fetchall()

                for row in rows:
                    values = []
                    for val in row:
                        if val is None:
                            values.append('NULL')
                        elif isinstance(val, str):
                            escaped = val.replace("'", "''").replace("\\", "\\\\")
                            values.append(f"'{escaped}'")
                        elif isinstance(val, bool):
                            values.append('TRUE' if val else 'FALSE')
                        elif isinstance(val, (int, float)):
                            values.append(str(val))
                        else:
                            escaped = str(val).replace("'", "''")
                            values.append(f"'{escaped}'")

                    f.write(f"INSERT INTO core.{table} ({', '.join(col_names)}) VALUES ({', '.join(values)});\n")

                total_rows += row_count
                f.write("\n")

            print(f"{row_count:,} rows")

        # Write footer
        f.write(f"\n-- ============================================\n")
        f.write(f"-- Backup Complete\n")
        f.write(f"-- Tables: {len(tables)}\n")
        f.write(f"-- Total rows: {total_rows:,}\n")
        f.write(f"-- ============================================\n")

        cur.close()

    conn.close()

    backup_size = os.path.getsize(backup_file)
    print(f"\n✅ Backup complete!")
    print(f"   File: {backup_file}")
    print(f"   Size: {backup_size:,} bytes ({backup_size/1024:.1f} KB)")
    print(f"   Tables: {len(tables)}")
    print(f"   Rows: {total_rows:,}")


if __name__ == "__main__":
    try:
        backup_database()
    except Exception as e:
        print(f"❌ Backup failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
