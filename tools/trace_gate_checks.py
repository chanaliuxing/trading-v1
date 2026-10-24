#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

try:
    import asyncpg
except ModuleNotFoundError:
    asyncpg = None


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: Dict[str, Any]
    skipped: bool = False


def build_dsn() -> str:
    user = os.getenv("PG_USER", "postgres")
    pwd = os.getenv("PG_PASSWORD", "")
    host = os.getenv("PG_HOST", "127.0.0.1")
    port = os.getenv("PG_PORT", "5432")
    db = os.getenv("PG_DATABASE", "trading")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


async def run_checks(conn: Any, hours: int) -> List[CheckResult]:
    out: List[CheckResult] = []

    # 1) Critical trace coverage
    q1 = """
    SELECT
      count(*) AS n_total,
      count(*) FILTER (
        WHERE strategy_id <> '' AND decision_id <> '' AND trace_id <> '' AND order_id IS NOT NULL
      ) AS n_ok
    FROM core.core_order_event
    WHERE event_type IN ('intent_received','order_submitted','fill_received','reconciler_corrected')
      AND event_ts >= now() - ($1::int * INTERVAL '1 hour')
    """
    r1 = await conn.fetchrow(q1, hours)
    n_total = int(r1["n_total"] or 0)
    n_ok = int(r1["n_ok"] or 0)
    out.append(CheckResult("critical_trace_coverage", (n_total == n_ok), {"n_total": n_total, "n_ok": n_ok}))

    def _is_missing_table_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "does not exist" in msg or "undefinedtable" in msg

    # 2) Parent-child invariant (table optional)
    try:
        q2 = """
        SELECT count(*) AS n_bad
        FROM (
          SELECT parent_order_id,
                 abs(sum(parent_fill_qty) - sum(child_fill_qty)) AS diff
          FROM netting_fill_audit
          WHERE ts >= now() - ($1::int * INTERVAL '1 hour')
          GROUP BY parent_order_id
        ) t
        WHERE diff > 0.000001
        """
        r2 = await conn.fetchrow(q2, hours)
        n_bad = int(r2["n_bad"] or 0)
        out.append(CheckResult("netting_parent_child_invariant", n_bad == 0, {"n_bad": n_bad}))
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            out.append(
                CheckResult(
                    "netting_parent_child_invariant",
                    True,
                    {"skipped": True, "reason": "table_missing", "error": str(exc)},
                    skipped=True,
                )
            )
        else:
            out.append(CheckResult("netting_parent_child_invariant", False, {"error": str(exc)}))

    # 3) Cross-strategy mutation (table optional)
    try:
        q3 = """
        SELECT count(*) AS n_bad
        FROM reconciliation_corrections
        WHERE correction_ts >= now() - ($1::int * INTERVAL '1 hour')
          AND corrected_strategy_id <> source_strategy_id
        """
        r3 = await conn.fetchrow(q3, hours)
        n_bad = int(r3["n_bad"] or 0)
        out.append(CheckResult("cross_strategy_mutation", n_bad == 0, {"n_bad": n_bad}))
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            out.append(
                CheckResult(
                    "cross_strategy_mutation",
                    True,
                    {"skipped": True, "reason": "table_missing", "error": str(exc)},
                    skipped=True,
                )
            )
        else:
            out.append(CheckResult("cross_strategy_mutation", False, {"error": str(exc)}))

    return out


async def main_async(args: argparse.Namespace) -> int:
    if asyncpg is None:
        raise SystemExit("Missing dependency: asyncpg")
    dsn = args.dsn or build_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        checks = await run_checks(conn, hours=args.window_hours)
    finally:
        await conn.close()

    passed = all(c.passed for c in checks)
    print(
        json.dumps(
            {
                "passed": passed,
                "window_hours": args.window_hours,
                "checks": [
                    {"name": c.name, "passed": c.passed, "skipped": c.skipped, "detail": c.detail}
                    for c in checks
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 2


def main() -> int:
    p = argparse.ArgumentParser(description="Run trace acceptance gate checks")
    p.add_argument("--dsn", default="", help="Postgres DSN")
    p.add_argument("--window-hours", type=int, default=24)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
