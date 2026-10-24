#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from libs.db.postgres import PostgresDB
from services.production.fill_attribution_service import adjudicate_fill_attribution


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(Path(args.repo_root).resolve() / ".env")
    pg = PostgresDB(min_conn=1, max_conn=2)
    await pg.connect()
    try:
        return await adjudicate_fill_attribution(
            pg=pg,
            event_key=args.event_key,
            decision=args.decision,
            adjudicated_by=args.adjudicated_by,
            matched_core_order_id=args.matched_core_order_id,
            matched_strategy_id=args.matched_strategy_id,
            note=args.note,
        )
    finally:
        await pg.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Adjudicate one fill attribution ledger event.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--event-key", required=True)
    parser.add_argument("--decision", choices=["manual_resolved", "manual_required", "ignored"], required=True)
    parser.add_argument("--adjudicated-by", required=True)
    parser.add_argument("--matched-core-order-id")
    parser.add_argument("--matched-strategy-id")
    parser.add_argument("--note")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
