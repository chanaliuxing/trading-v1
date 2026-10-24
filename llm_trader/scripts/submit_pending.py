#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from llm_trader.pending_orders_manager import PendingOrdersManager
from llm_trader.trading.ibkr_executor import IBKRExecutor, OrderAction
from llm_trader.broker_map_store import exists_pending, append_mapping


async def submit_pending(base: str | None = None, host: str = "127.0.0.1", port: int = 4002) -> dict:
    pom = PendingOrdersManager()
    pom.load_orders()

    # Connect to IBKR
    execu = IBKRExecutor(host=host, port=port)
    await execu.connect()

    placed = 0
    skipped = 0
    failed = 0

    try:
        for o in pom.get_all_orders():
            # Skip expired (cleanup was done on load, but double check)
            if o.is_expired():
                continue
            # Skip if already submitted
            if exists_pending(o.order_id, base):
                skipped += 1
                continue
            # Place limit order
            side = OrderAction(o.action.upper())
            trade = None
            # If OCO specified, place bracket
            if (o.take_profit_bps or o.stop_loss_bps):
                res = await execu.place_bracket_limit(
                    o.symbol, side, o.quantity, o.limit_price,
                    take_profit_bps=o.take_profit_bps, stop_loss_bps=o.stop_loss_bps
                )
                trade = res[0] if res else None
            else:
                trade = await execu.place_limit_order(o.symbol, side, o.quantity, o.limit_price)

            if trade is not None:
                append_mapping({
                    "pending_id": o.order_id,
                    "broker_order_id": getattr(trade.order, 'orderId', None),
                    "symbol": o.symbol,
                    "side": o.action.upper(),
                    "qty": o.quantity,
                    "limit": o.limit_price,
                    "reprices": 0,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }, base)
                placed += 1
            else:
                failed += 1
    finally:
        await execu.disconnect()

    return {"placed": placed, "skipped": skipped, "failed": failed}


def main():
    ap = argparse.ArgumentParser(description="Submit pending orders to IBKR (P1 minimal)")
    ap.add_argument("--base", default=None, help="Base runs dir for llm_trader")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    args = ap.parse_args()

    res = asyncio.run(submit_pending(args.base, args.host, args.port))
    print(res)


if __name__ == "__main__":
    main()
