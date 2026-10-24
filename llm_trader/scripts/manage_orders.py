#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from llm_trader.state_store import load_plan
from llm_trader.broker_map_store import load_map, save_map
from llm_trader.trading.ibkr_executor import IBKRExecutor, OrderAction


def _adjust_for_symbol(plan: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    d = (plan.get("directives") or {}).get(symbol) or {}
    adj = d.get("adjust") or {}
    return {
        "cancel_ttl_sec": int(adj.get("cancel_ttl_sec", 600) or 600),
        "replace_step_bps": int(adj.get("replace_step_bps", 5) or 5),
        "max_reprices": int(adj.get("max_reprices", 3) or 3),
    }


async def manage_orders(base: str | None = None, host: str = "127.0.0.1", port: int = 4002) -> dict:
    plan = load_plan(base) or {}
    mappings = load_map(base)
    if not mappings:
        return {"status": "no_open_mappings"}

    exe = IBKRExecutor(host=host, port=port)
    await exe.connect()

    cancelled = 0
    replaced = 0
    errors = 0

    try:
        # Get open trades from IBKR
        open_trades = await exe.get_open_orders()
        # Build index orderId -> trade
        by_id = {getattr(t.order, 'orderId', None): t for t in open_trades}

        for m in list(mappings):
            try:
                oid = m.get("broker_order_id")
                pend_id = m.get("pending_id")
                sym = m.get("symbol")
                side = m.get("side")
                ts_str = m.get("ts")
                reprices = int(m.get("reprices", 0) or 0)
                if oid is None or sym is None:
                    continue
                trade = by_id.get(oid)
                if trade is None:
                    # order not open anymore; skip
                    continue
                # TTL cancel policy
                adj = _adjust_for_symbol(plan, sym)
                ttl = int(adj.get("cancel_ttl_sec", 600))
                elapsed = 0
                try:
                    created = datetime.fromisoformat(ts_str.replace('Z', '+00:00')) if ts_str else None
                    if created:
                        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
                except Exception:
                    pass
                if elapsed > ttl:
                    ok = await exe.cancel_order(trade)
                    if ok:
                        cancelled += 1
                        # remove mapping entry by setting orderId None
                        m["broker_order_id"] = None
                        continue
                # Replace step policy
                if reprices < int(adj.get("max_reprices", 3)):
                    # get current price and compute new lmt toward mid by step bps
                    mid = await exe.get_current_price(sym)
                    if mid is None:
                        continue
                    old = float(m.get("limit", 0.0) or 0.0)
                    step = float(adj.get("replace_step_bps", 5))/10000.0
                    if side == 'BUY':
                        if (mid - old) / max(mid, 1e-6) > step:
                            newp = old * (1.0 + step)
                            ok = await exe.replace_limit_order(trade, newp)
                            if ok:
                                replaced += 1
                                m["limit"] = newp
                                m["reprices"] = reprices + 1
                    else:
                        if (old - mid) / max(mid, 1e-6) > step:
                            newp = old * (1.0 - step)
                            ok = await exe.replace_limit_order(trade, newp)
                            if ok:
                                replaced += 1
                                m["limit"] = newp
                                m["reprices"] = reprices + 1
            except Exception:
                errors += 1
                continue
        save_map(mappings, base)
    finally:
        await exe.disconnect()

    return {"cancelled": cancelled, "replaced": replaced, "errors": errors}


def main():
    ap = argparse.ArgumentParser(description="Manage live orders: cancel TTL and replace toward mid (P2 minimal)")
    ap.add_argument("--base", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    args = ap.parse_args()

    res = asyncio.run(manage_orders(args.base, args.host, args.port))
    print(res)


if __name__ == "__main__":
    main()

