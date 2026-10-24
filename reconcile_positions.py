import asyncio
from libs.db.postgres import PostgresDB
from services.ibkr.ib_connection_manager import get_ib_connection_manager
from datetime import datetime

async def reconcile_positions():
    pg = PostgresDB()
    ib_mgr = get_ib_connection_manager()
    ib = await ib_mgr.get_market_ib(host='127.0.0.1', port=4002)
    
    print("📡 Fetching real positions from IBKR...")
    real_pos = {p.contract.symbol: p.position for p in ib.positions() if abs(p.position) > 0}
    
    for sym, qty in real_pos.items():
        print(f"🔎 Checking {sym} (Qty: {qty})...")
        # Check if DB has a matching filled record for today
        rows = await pg.fetchall("""
            SELECT order_id, broker_order_id, state 
            FROM core.core_order 
            WHERE symbol = $1 AND state = 'FILLED' AND created_at > now() - interval '24 hours'
        """, sym)
        
        if not rows:
            print(f"⚠️  ORPHAN POSITION: {sym} has {qty} shares but no FILLED record in DB for last 24h.")
            # Auto-repair: mark the most recent NEW/CANCELLED order as FILLED if qty matches
            await pg.execute("""
                UPDATE core.core_order 
                SET state = 'FILLED', 
                    note = 'Auto-reconciled by position auditor'
                WHERE symbol = $1 AND state != 'FILLED' AND qty = $2
                AND created_at > now() - interval '24 hours'
            """, sym, qty)
            print(f"✅ Auto-repair attempt complete for {sym}.")
        else:
            print(f"✅ {sym} is already aligned in DB.")

if __name__ == "__main__":
    asyncio.run(reconcile_positions())
