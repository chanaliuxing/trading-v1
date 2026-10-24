#!/usr/bin/env python3
"""Cancel pending options hedge orders.

This script cancels unfilled hedge orders from the core.core_order table.
Useful for:
- Cleaning up stale limit orders that won't fill
- Emergency order cancellation
- End-of-day cleanup

Usage:
    # List pending hedge orders (dry-run)
    python scripts/ai_trader/risk/cancel_hedge_orders.py --list

    # Cancel specific order by ID
    python scripts/ai_trader/risk/cancel_hedge_orders.py --order-id 12345

    # Cancel all pending hedge orders (DANGEROUS)
    python scripts/ai_trader/risk/cancel_hedge_orders.py --all --confirm

Safety:
- Requires explicit confirmation for --all
- Only cancels orders in NEW/SUBMITTED/PARTIAL_FILLED states
- Does not cancel FILLED orders
- Logs all cancellations
"""

from __future__ import annotations
import argparse
import asyncio
from datetime import datetime
from typing import List, Dict, Any

# Database imports
try:
    import asyncpg
except ImportError:
    print("❌ asyncpg not installed. Run: pip install asyncpg")
    exit(1)


async def get_db_connection():
    """Get PostgreSQL connection."""
    import os

    # Load from environment with fallback
    db_password = os.environ.get('TRADING_DB_PASSWORD')

    if not db_password:
        # Try to load from config.yml
        try:
            import yaml
            with open('config.yml', 'r') as f:
                config = yaml.safe_load(f)
                db_password = config.get('database', {}).get('password', '')
        except Exception:
            pass

    if not db_password:
        raise RuntimeError(
            "Database password not found. Set TRADING_DB_PASSWORD environment variable "
            "or configure in config.yml"
        )

    return await asyncpg.connect(
        host="localhost",
        port=5432,
        database="trading_db",
        user="trading_user",
        password=db_password,
    )


async def list_pending_hedge_orders(conn: asyncpg.Connection) -> List[Dict[str, Any]]:
    """List all pending hedge orders.

    Args:
        conn: Database connection

    Returns:
        List of order dictionaries
    """
    query = """
    SELECT
        id,
        symbol,
        side,
        order_type,
        qty,
        limit_price,
        state,
        created_at,
        note
    FROM core.core_order
    WHERE strategy_id = 'hedge_overlay'
      AND order_type = 'OPT_LMT'
      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL_FILLED')
    ORDER BY created_at DESC
    """

    rows = await conn.fetch(query)
    return [dict(row) for row in rows]


async def cancel_order_by_id(conn: asyncpg.Connection, order_id: int) -> bool:
    """Cancel specific order.

    Args:
        conn: Database connection
        order_id: Order ID to cancel

    Returns:
        True if canceled successfully
    """
    # Check if order exists and is cancelable
    check_query = """
    SELECT id, symbol, state
    FROM core.core_order
    WHERE id = $1
      AND strategy_id = 'hedge_overlay'
      AND order_type = 'OPT_LMT'
      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL_FILLED')
    """

    row = await conn.fetchrow(check_query, order_id)
    if not row:
        print(f"❌ Order {order_id} not found or not cancelable")
        return False

    # Update state to CANCELED
    update_query = """
    UPDATE core.core_order
    SET
        state = 'CANCELED',
        updated_at = $1,
        note = COALESCE(note || ' ', '') || '[MANUAL_CANCEL]'
    WHERE id = $2
    """

    await conn.execute(update_query, datetime.now(), order_id)
    print(f"✅ Canceled order {order_id} ({row['symbol']})")

    # TODO: Also cancel in IBKR if order was submitted
    # This requires IBKR connection and order_id mapping

    return True


async def cancel_all_hedge_orders(conn: asyncpg.Connection) -> int:
    """Cancel all pending hedge orders.

    Args:
        conn: Database connection

    Returns:
        Number of orders canceled
    """
    orders = await list_pending_hedge_orders(conn)

    if not orders:
        print("ℹ️ No pending hedge orders to cancel")
        return 0

    canceled = 0
    for order in orders:
        success = await cancel_order_by_id(conn, order['id'])
        if success:
            canceled += 1

    return canceled


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Cancel pending options hedge orders",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List pending hedge orders (no cancellation)",
    )

    parser.add_argument(
        "--order-id",
        type=int,
        help="Cancel specific order by ID",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Cancel ALL pending hedge orders (requires --confirm)",
    )

    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm cancellation (required for --all)",
    )

    args = parser.parse_args()

    # Connect to database
    try:
        conn = await get_db_connection()
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return

    try:
        if args.list:
            # List only
            print("📋 Pending Hedge Orders:\n")
            orders = await list_pending_hedge_orders(conn)

            if not orders:
                print("ℹ️ No pending orders")
                return

            print(f"{'ID':<8} {'Symbol':<25} {'Side':<6} {'Qty':<5} {'Price':<10} {'State':<15} {'Created'}")
            print("-" * 100)

            for order in orders:
                print(
                    f"{order['id']:<8} "
                    f"{order['symbol']:<25} "
                    f"{order['side']:<6} "
                    f"{order['qty']:<5} "
                    f"${order['limit_price']:<9.2f} "
                    f"{order['state']:<15} "
                    f"{order['created_at'].isoformat()}"
                )

            print(f"\nTotal: {len(orders)} pending orders")

        elif args.order_id:
            # Cancel specific order
            print(f"🗑️ Canceling order {args.order_id}...")
            await cancel_order_by_id(conn, args.order_id)

        elif args.all:
            # Cancel all
            if not args.confirm:
                print("❌ --all requires --confirm flag for safety")
                print("   Run with: --all --confirm")
                return

            print("⚠️ WARNING: This will cancel ALL pending hedge orders")
            confirm = input("Type 'CANCEL ALL' to proceed: ")

            if confirm != "CANCEL ALL":
                print("❌ Aborted")
                return

            print("\n🗑️ Canceling all pending hedge orders...")
            canceled = await cancel_all_hedge_orders(conn)
            print(f"\n✅ Canceled {canceled} orders")

        else:
            parser.print_help()

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
