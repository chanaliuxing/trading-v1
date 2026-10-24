import asyncio
import json
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from libs.db.postgres import PostgresDB
from services.ibkr.ib_connection_manager import get_ib_connection_manager

ET = ZoneInfo("America/New_York")

async def run_rigorous_tests():
    pg = PostgresDB()
    print("="*60)
    print(f"🚀 STARTING SYSTEM HARDENING VERIFICATION @ {datetime.now(ET)}")
    print("="*60)

    # --- TEST 1: DB CONSTRAINT RELAXATION ---
    print("\n[Test 1] Testing Broker ID Reuse (Constraint Relaxation)...")
    try:
        shared_id = f"REUSED_{int(time.time())}"
        for i in range(3):
            ikey = f"idempotency_{uuid.uuid4()}"
            await pg.execute("""
                INSERT INTO core.core_order (order_id, strategy_id, symbol, side, order_type, qty, state, broker_order_id, idempotency_key)
                VALUES (gen_random_uuid(), 'qqq_intraday_001', 'VERIFY', 'BUY', 'MKT', 1, 'CANCELLED', $1, $2)
            """, shared_id, ikey)
        print(f"✅ Success: Successfully inserted 3 records with identical broker_order_id: {shared_id}")
        await pg.execute("DELETE FROM core.core_order WHERE symbol = 'VERIFY'")
    except Exception as e:
        print(f"❌ FAIL: DB Constraint still blocking ID reuse: {e}")

    # --- TEST 2: POSITION PARITY AUDIT ---
    print("\n[Test 2] Auditing Position Parity (DB vs IBKR)...")
    try:
        ib_mgr = get_ib_connection_manager()
        # Check connection first
        ib = await ib_mgr.get_market_ib(host='127.0.0.1', port=4002)
        ib_pos = {p.contract.symbol: p.position for p in ib.positions() if abs(p.position) > 0}
        
        db_rows = await pg.fetchall("""
            SELECT symbol, SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END) as net_qty
            FROM core.core_order
            WHERE state = 'FILLED' AND created_at > now() - interval '24 hours'
            GROUP BY symbol
        """)
        db_pos = {r['symbol']: float(r['net_qty']) for r in db_rows if abs(r['net_qty']) > 0}
        
        all_syms = set(ib_pos.keys()) | set(db_pos.keys())
        if not all_syms:
            print("✓ No positions active in both IB and DB.")
        else:
            mismatches = 0
            for sym in all_syms:
                i_qty = ib_pos.get(sym, 0.0)
                d_qty = db_pos.get(sym, 0.0)
                if abs(i_qty - d_qty) > 1e-5:
                    print(f"⚠️  MISMATCH detected for {sym}: IB={i_qty}, DB_last_24h={d_qty}")
                    mismatches += 1
                else:
                    print(f"✅ Parity OK for {sym}: {i_qty}")
            if mismatches == 0:
                print("✅ All positions are perfectly aligned.")
    except Exception as e:
        print(f"⚠️ Position audit partially failed (likely connection): {e}")

    # --- TEST 3: PERSISTENCE FRESHNESS ---
    print("\n[Test 3] Checking State Persistence Freshness...")
    state_p = Path("runtime/state/qqq_intraday_qqq_intraday_001.json")
    if state_p.exists():
        mtime = datetime.fromtimestamp(state_p.stat().st_mtime, tz=ET)
        age = (datetime.now(ET) - mtime).total_seconds()
        print(f"📄 JSON State File: {state_p.name}")
        print(f"   Last Update: {mtime} ({age:.1f}s ago)")
        if age < 60:
            print("✅ State is being actively persisted by the strategy.")
        else:
            print("❌ WARNING: State file is stale (>60s). Strategy might be stuck!")
    else:
        print("❌ FAIL: Persistent state file missing!")

    # --- TEST 4: LOG INTEGRITY SCAN ---
    print("\n[Test 4] Scanning for Critical Error Patterns in Logs...")
    log_p = Path("logs/launchd_run_system.err.log")
    if log_p.exists():
        try:
            content = log_p.read_text().splitlines()[-50:]
            has_perm_error = any("Operation not permitted" in line for line in content)
            if has_perm_error:
                print("❌ FAIL: System is still reporting Permission Denied in recent logs!")
            else:
                print("✅ Success: No recent permission errors found.")
        except Exception as e:
            print(f"⚠️ Log scan failed: {e}")

    print("\n" + "="*60)
    print("🏁 RIGOROUS TESTING COMPLETE")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(run_rigorous_tests())
