#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.config import config
from libs.db.postgres import PostgresDB
from services.exec.adapters.ib_adapter import IBInsyncAdapter
from services.reconciliation.runtime_reconciler import RuntimeReconciler


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run RuntimeReconciler.run_once() as a one-shot repair.")
    p.add_argument("--client-id", type=int, default=2991, help="IBKR clientId for the probe connection.")
    p.add_argument("--ghost-grace-seconds", type=int, default=90)
    p.add_argument("--auto-cleanup-ghosts", action="store_true", default=True)
    p.add_argument("--no-auto-cleanup-ghosts", action="store_false", dest="auto_cleanup_ghosts")
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()

    pg = PostgresDB()
    await pg.connect()

    ib_cfg = config.ib_config
    adapter = IBInsyncAdapter(
        host=ib_cfg["host"],
        port=int(ib_cfg["port"]),
        client_id=int(args.client_id),
    )
    await adapter.start()
    try:
        recon = RuntimeReconciler(
            pg=pg,
            ib_adapter=adapter,
            reservation_service=None,
            decision_finalizer=None,
            interval_seconds=60,
            auto_cleanup_ghosts=bool(args.auto_cleanup_ghosts),
            ghost_grace_seconds=int(args.ghost_grace_seconds),
            notification_manager=None,
            order_event_enabled=False,
            audit_writer=None,
        )
        report = await recon.run_once()
        payload = {
            "db_open_count": int(report.db_open_count),
            "ib_open_count": int(report.ib_open_count),
            "ghosts_found": int(report.ghosts_found),
            "orphans_found": int(report.orphans_found),
            "mismatches": int(report.mismatches),
            "duration_ms": float(report.duration_ms),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        await adapter.stop()
        await pg.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

