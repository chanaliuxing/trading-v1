#!/usr/bin/env python3
"""
Register a strategy in core.core_strategy + core.core_strategy_ledger.

Why:
  ReservationService uses stored procedures that require the strategy to exist
  in core.core_strategy (else STRATEGY_NOT_FOUND).

Usage:
  PYTHONPATH=. ./venv/bin/python scripts/register_strategy_ledger.py \
    --strategy-id qqq_0dte_opt_live_001 \
    --name qqq_0dte_opt_live_001 \
    --capital-limit-usd 20000 \
    --reserve-buffer-bps 10 \
    --enabled 0
"""

from __future__ import annotations

import argparse
import asyncio

from libs.db.postgres import PostgresDB


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-dsn", default=None, help="Override Postgres DSN (else uses env/config defaults)")
    ap.add_argument("--strategy-id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--capital-limit-usd", type=float, required=True)
    ap.add_argument("--reserve-buffer-bps", type=int, default=10)
    ap.add_argument("--enabled", type=int, default=0)
    args = ap.parse_args()

    pg = PostgresDB(dsn=args.pg_dsn) if args.pg_dsn else PostgresDB()
    await pg.connect()
    try:
        await pg.execute(
            """
            INSERT INTO core.core_strategy (
                strategy_id, name, capital_limit_usd, reserve_buffer_bps, is_enabled, created_at
            ) VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (strategy_id) DO UPDATE
            SET name = EXCLUDED.name,
                capital_limit_usd = EXCLUDED.capital_limit_usd,
                reserve_buffer_bps = EXCLUDED.reserve_buffer_bps,
                is_enabled = EXCLUDED.is_enabled
            """,
            str(args.strategy_id),
            str(args.name),
            float(args.capital_limit_usd),
            int(args.reserve_buffer_bps),
            bool(int(args.enabled)),
        )
        await pg.execute(
            """
            INSERT INTO core.core_strategy_ledger (
                strategy_id, reserved_usd, realized_pnl_usd, updated_at, version
            ) VALUES ($1, 0, 0, now(), 0)
            ON CONFLICT (strategy_id) DO NOTHING
            """,
            str(args.strategy_id),
        )
        print(f"OK registered strategy_id={args.strategy_id}")
    finally:
        await pg.close()


if __name__ == "__main__":
    asyncio.run(main())

