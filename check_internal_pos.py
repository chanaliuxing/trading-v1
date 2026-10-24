import asyncio
from services.ibkr.ib_connection_manager import get_ib_connection_manager
import os

async def check_internal_pos():
    ib_mgr = get_ib_connection_manager()
    ib = await ib_mgr.get_market_ib(host='127.0.0.1', port=4002)
    print("📡 Fetching positions from IB via internal manager...")
    positions = ib.positions()
    for p in positions:
        print(f"Contract: {p.contract.symbol} Qty: {p.position} AvgCost: {p.avgCost}")

if __name__ == "__main__":
    asyncio.run(check_internal_pos())
