#!/usr/bin/env python3
"""
Check IBKR option positions vs local DB records to prevent ghost positions.

This script is intentionally conservative:
- If it cannot connect to IB or Postgres, it exits non-zero.
- It only reasons about QQQ put vertical spreads recorded in `trading.option_spread_trades`.

Usage:
  venv/bin/python scripts/check_positions_consistency.py --symbol QQQ
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

import asyncpg

# Ensure repo root is on sys.path when invoked via `python scripts/...` or `make`.
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.logger import get_logger
from services.exec.adapters.ib_adapter import init_global_ib_adapter

logger = get_logger(__name__)


@dataclass(frozen=True)
class SpreadKey:
    symbol: str
    expiry: str
    short_strike: float
    long_strike: float


def _pg_cfg() -> Dict[str, str | int]:
    return {
        "host": os.getenv("PG_HOST", "localhost"),
        "port": int(os.getenv("PG_PORT", "5432")),
        "database": os.getenv("PG_DB", "test"),
        "user": os.getenv("PG_USER", "postgres"),
        "password": os.getenv("PG_PASSWORD", ""),
    }


async def _fetch_open_spreads(conn: asyncpg.Connection, symbol: str) -> List[SpreadKey]:
    rows = await conn.fetch(
        """
        WITH opens AS (
            SELECT symbol, expiry, short_strike, long_strike, max(ts) AS opened_ts
            FROM trading.option_spread_trades
            WHERE action = 'OPEN_FILLED' AND symbol = $1
            GROUP BY symbol, expiry, short_strike, long_strike
        ),
        closes AS (
            SELECT symbol, expiry, short_strike, long_strike, max(ts) AS closed_ts
            FROM trading.option_spread_trades
            WHERE action IN ('EXIT_TP_FILLED','EXIT_SL_FILLED','CLOSE_FINAL','CLOSE_FILLED')
              AND symbol = $1
            GROUP BY symbol, expiry, short_strike, long_strike
        )
        SELECT o.symbol, o.expiry, o.short_strike, o.long_strike
        FROM opens o
        LEFT JOIN closes c
          ON c.symbol=o.symbol AND c.expiry=o.expiry AND c.short_strike=o.short_strike AND c.long_strike=o.long_strike
        WHERE c.closed_ts IS NULL OR c.closed_ts < o.opened_ts
        ORDER BY o.expiry DESC, o.short_strike DESC
        """,
        symbol,
    )
    return [
        SpreadKey(
            symbol=r["symbol"],
            expiry=r["expiry"],
            short_strike=float(r["short_strike"]),
            long_strike=float(r["long_strike"]),
        )
        for r in rows
    ]


def _normalize_ib_expiry(last_trade_date_or_contract_month: str) -> str:
    # IB usually returns YYYYMMDD for options
    if len(last_trade_date_or_contract_month) == 8 and last_trade_date_or_contract_month.isdigit():
        return f"{last_trade_date_or_contract_month[0:4]}-{last_trade_date_or_contract_month[4:6]}-{last_trade_date_or_contract_month[6:8]}"
    return last_trade_date_or_contract_month


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="QQQ")
    args = parser.parse_args()

    symbol = args.symbol.upper()

    cfg = _pg_cfg()
    if not cfg["password"]:
        logger.error("pg_password_missing", hint="Set PG_PASSWORD in .env before running reconciliation")
        return 2

    try:
        conn = await asyncpg.connect(**cfg)
    except Exception as exc:
        logger.error("pg_connect_failed", error=str(exc))
        return 2

    try:
        open_spreads = await _fetch_open_spreads(conn, symbol)
    except Exception as exc:
        logger.error("pg_query_failed", error=str(exc))
        await conn.close()
        return 2
    finally:
        await conn.close()

    # Pull IB positions
    try:
        adapter = await init_global_ib_adapter()
        positions = adapter.ib.positions()
    except Exception as exc:
        logger.error("ib_connect_or_positions_failed", error=str(exc))
        return 2

    # Aggregate IB option positions for the symbol puts.
    # Keyed by (expiry_yyyy-mm-dd, strike) -> net position (short negative, long positive)
    ib_legs: Dict[Tuple[str, float], float] = defaultdict(float)
    for p in positions:
        c = p.contract
        if getattr(c, "secType", None) != "OPT":
            continue
        if getattr(c, "symbol", "").upper() != symbol:
            continue
        if getattr(c, "right", "").upper() != "P":
            continue
        expiry = _normalize_ib_expiry(getattr(c, "lastTradeDateOrContractMonth", ""))
        strike = float(getattr(c, "strike", 0.0))
        ib_legs[(expiry, strike)] += float(p.position)

    expected_missing = []
    for s in open_spreads:
        short_qty = ib_legs.get((s.expiry, s.short_strike), 0.0)
        long_qty = ib_legs.get((s.expiry, s.long_strike), 0.0)
        if short_qty >= 0 or long_qty <= 0:
            expected_missing.append((s, short_qty, long_qty))

    # Detect "unknown" IB positions not represented by any open spread.
    open_leg_set = set()
    for s in open_spreads:
        open_leg_set.add((s.expiry, s.short_strike))
        open_leg_set.add((s.expiry, s.long_strike))

    unknown_ib = []
    for (expiry, strike), qty in sorted(ib_legs.items()):
        if qty == 0:
            continue
        if (expiry, strike) not in open_leg_set:
            unknown_ib.append((expiry, strike, qty))

    if not open_spreads and not ib_legs:
        logger.info("positions_consistent", symbol=symbol, message="No open spreads and no IB option positions")
        return 0

    if expected_missing or unknown_ib:
        logger.error(
            "positions_inconsistent",
            symbol=symbol,
            open_spreads=len(open_spreads),
            ib_legs=len(ib_legs),
        )
        for s, short_qty, long_qty in expected_missing:
            logger.error(
                "spread_missing_in_ib",
                symbol=s.symbol,
                expiry=s.expiry,
                short_strike=s.short_strike,
                long_strike=s.long_strike,
                ib_short_qty=short_qty,
                ib_long_qty=long_qty,
            )
        for expiry, strike, qty in unknown_ib:
            logger.error("unknown_ib_leg", symbol=symbol, expiry=expiry, strike=strike, qty=qty)
        return 1

    logger.info("positions_consistent", symbol=symbol, open_spreads=len(open_spreads), ib_legs=len(ib_legs))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
