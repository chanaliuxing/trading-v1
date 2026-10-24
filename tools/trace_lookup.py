#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List

try:
    import asyncpg
except ModuleNotFoundError:
    asyncpg = None


def parse_window(hours: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(1, hours))
    return start, end


def build_dsn() -> str:
    user = os.getenv("PG_USER", "postgres")
    pwd = os.getenv("PG_PASSWORD", "")
    host = os.getenv("PG_HOST", "127.0.0.1")
    port = os.getenv("PG_PORT", "5432")
    db = os.getenv("PG_DATABASE", "trading")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


async def run(args: argparse.Namespace) -> int:
    if asyncpg is None:
        raise SystemExit("Missing dependency: asyncpg")

    if args.from_ts and args.to_ts:
        start = datetime.fromisoformat(args.from_ts)
        end = datetime.fromisoformat(args.to_ts)
    else:
        start, end = parse_window(args.window_hours)

    dsn = args.dsn or build_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        clauses: List[str] = ["event_ts >= $1", "event_ts <= $2"]
        params: List[Any] = [start, end]

        if args.symbol:
            params.append(args.symbol.upper())
            clauses.append(f"symbol = ${len(params)}")
        if args.strategy_id:
            params.append(args.strategy_id)
            clauses.append(f"strategy_id = ${len(params)}")
        if args.order_id:
            params.append(args.order_id)
            clauses.append(f"order_id::text = ${len(params)}")
        if args.broker_order_id:
            params.append(args.broker_order_id)
            clauses.append(f"broker_order_id = ${len(params)}")
        if args.trace_id:
            params.append(args.trace_id)
            clauses.append(f"trace_id = ${len(params)}")

        limit = max(1, min(2000, args.limit))
        params.append(limit)

        sql = f"""
        SELECT
            event_ts,
            event_type,
            strategy_id,
            strategy_run_id,
            trigger_id,
            decision_id,
            intent_id,
            order_id::text AS order_id,
            broker_order_id,
            parent_order_id::text AS parent_order_id,
            trace_id,
            causation_id,
            symbol,
            payload
        FROM core.core_order_event
        WHERE {' AND '.join(clauses)}
        ORDER BY event_ts DESC
        LIMIT ${len(params)}
        """

        rows = await conn.fetch(sql, *params)
        out = []
        for r in rows:
            item = dict(r)
            if isinstance(item.get("event_ts"), datetime):
                item["event_ts"] = item["event_ts"].isoformat()
            out.append(item)
        print(json.dumps({"count": len(out), "rows": out}, ensure_ascii=False, indent=2, default=str))
    finally:
        await conn.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Trace lookup from core.core_order_event")
    p.add_argument("--dsn", default="", help="Postgres DSN")
    p.add_argument("--window-hours", type=int, default=24)
    p.add_argument("--from-ts", default="", help="ISO timestamp")
    p.add_argument("--to-ts", default="", help="ISO timestamp")
    p.add_argument("--symbol", default="")
    p.add_argument("--strategy-id", default="")
    p.add_argument("--order-id", default="")
    p.add_argument("--broker-order-id", default="")
    p.add_argument("--trace-id", default="")
    p.add_argument("--limit", type=int, default=200)
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
