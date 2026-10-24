#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check LLM Trader realtime readiness per symbol using Redis 1s bars.

Logic:
- For each symbol, read key `sfs:1s:{symbol}` and check latest item's timestamp freshness.
- Optionally check list length to ensure minimal context is present.

This script does NOT query ClickHouse. LLM Trader decisions are built from
in-memory realtime aggregation of Redis 1s bars.
"""
import asyncio
import os
import json
import argparse
from typing import List, Dict, Any

import redis.asyncio as redis


def _default_symbols() -> List[str]:
    try:
        # Reuse LLM Trader config loader (falls back to AAPL/TSLA/NVDA)
        from llm_trader.llm_trader.utils.config import Config as LLMConfig
        cfg = LLMConfig()
        return cfg.trading_symbols
    except Exception:
        return ["AAPL", "TSLA", "NVDA"]


async def check_symbol(r: redis.Redis, symbol: str, stale_sec: int, min_len: int) -> Dict[str, Any]:
    key = f"sfs:1s:{symbol}"
    try:
        # Latest item is index 0 (producer pushes new items to head)
        latest = await r.lindex(key, 0)
        llen = await r.llen(key)

        if not latest:
            return {"symbol": symbol, "ok": False, "reason": "no_data", "llen": llen}

        bar = json.loads(latest)
        ts_val = bar.get("ts")
        if not isinstance(ts_val, (int, float)):
            return {"symbol": symbol, "ok": False, "reason": "invalid_ts_format", "llen": llen}

        import time
        now_ms = int(time.time() * 1000)
        age_sec = (now_ms - int(ts_val)) / 1000.0

        ok = (age_sec <= stale_sec) and (llen >= min_len)
        return {
            "symbol": symbol,
            "ok": ok,
            "age_sec": round(age_sec, 2),
            "llen": int(llen),
            "reason": None if ok else ("stale" if age_sec > stale_sec else "insufficient_context")
        }

    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": str(e)}


async def main():
    ap = argparse.ArgumentParser(description="Check LLM Trader realtime readiness (Redis 1s bars)")
    ap.add_argument("--redis-url", default=os.getenv("STATEBUS_REDIS", "redis://127.0.0.1:6379/0"))
    ap.add_argument("--symbols", nargs="*", default=None, help="Symbols to check (default: from config.yml universe)")
    ap.add_argument("--stale-sec", type=int, default=10, help="Max allowed age of latest 1s bar (sec)")
    ap.add_argument("--min-1s", type=int, default=30, help="Minimum 1s bars in Redis list")
    ap.add_argument("--json", action="store_true", help="Print JSON output")
    args = ap.parse_args()

    symbols = args.symbols if args.symbols else _default_symbols()
    r = await redis.from_url(args.redis_url)

    results = []
    for sym in symbols:
        res = await check_symbol(r, sym, stale_sec=args.stale_sec, min_len=args.min_1s)
        results.append(res)

    await r.close()

    ok_overall = all(r.get("ok") for r in results)

    if args.json:
        print(json.dumps({"ok": ok_overall, "results": results}, indent=2))
    else:
        print("=" * 80)
        print("LLM Trader Realtime Readiness (Redis 1s)")
        print("=" * 80)
        for r in results:
            status = "OK" if r.get("ok") else "FAIL"
            age = r.get("age_sec")
            llen = r.get("llen")
            reason = r.get("reason") or r.get("error") or ""
            print(f"{r['symbol']:<6} {status:<5} age={age!s:<6} llen={llen!s:<6} {reason}")
        print("-" * 80)
        print(f"OVERALL: {'OK' if ok_overall else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())

