#!/usr/bin/env python3
"""
Check current IBKR positions
"""

import asyncio
import sys
from pathlib import Path

# Enable nested event loops for ib_insync compatibility
import nest_asyncio
nest_asyncio.apply()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from llm_trader.trading.ibkr_executor import IBKRExecutor
from llm_trader.utils.config import Config


async def check_positions():
    config = Config()
    executor = IBKRExecutor(host=config.ib_host, port=config.ib_port)

    try:
        await executor.connect()

        print("=" * 60)
        print("IBKR Account Status")
        print("=" * 60)

        # Account summary
        account = await executor.get_account_summary()
        print(f"\n💰 Cash: ${account['cash']:,.2f}")
        print(f"📊 Equity: ${account['equity']:,.2f}")
        print(f"💵 Buying Power: ${account['buying_power']:,.2f}")

        # Positions
        positions = await executor.get_positions()
        print(f"\n📦 Positions: {len(positions)}")
        if positions:
            for symbol, qty in positions.items():
                print(f"  - {symbol}: {qty} shares")
        else:
            print("  (No positions)")

        print("=" * 60)

    finally:
        await executor.disconnect()


if __name__ == "__main__":
    asyncio.run(check_positions())
