#!/usr/bin/env python3
"""
Aggregate simple heuristics from recent ML episodes into trading.knowledge_heuristics.

This is intentionally lightweight:
- Checks for source table existence before querying.
- Creates knowledge_heuristics if missing.
- Inserts a few coarse heuristics (per strategy) using low-memory settings.
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import clickhouse_connect  # type: ignore
from libs.logger import get_logger

log = get_logger(__name__)


def _get_client():
    return clickhouse_connect.get_client(
        host="localhost",
        port=8123,
        database="trading",
        settings={
            "max_threads": 2,
            "max_memory_usage": int(200_000_000),
            "use_uncompressed_cache": 0,
        },
    )


def _table_exists(cli, database: str, table: str) -> bool:
    try:
        res = cli.query(
            "SELECT 1 FROM system.tables WHERE database=%(db)s AND name=%(t)s LIMIT 1",
            parameters={"db": database, "t": table},
        )
        return bool(res.result_rows)
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("table_exists_check_failed", error=str(exc), table=table)
        return False


def _ensure_heuristics_table(cli) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS trading.knowledge_heuristics (
        id UUID DEFAULT generateUUIDv4(),
        created_at DateTime DEFAULT now(),
        condition String,
        advice String,
        metrics Map(String, Float32),
        confidence Float32,
        status Enum('ACTIVE','DEPRECATED') DEFAULT 'ACTIVE',
        source_experiment_id String
    ) ENGINE = MergeTree()
    ORDER BY (created_at, confidence)
    """
    cli.command(sql)


def _load_recent_strategy_stats(cli, days: int = 30) -> List[Dict[str, Any]]:
    if not _table_exists(cli, "trading", "ai_trader_memory_episodes"):
        log.info("episodes_table_missing_skip")
        return []
    q = """
    SELECT
        coalesce(params_id, 'unknown') AS params_id,
        avg(oos_sharpe) AS sharpe,
        count() AS n
    FROM trading.ai_trader_memory_episodes
    WHERE created_at >= now() - INTERVAL %(days)s DAY
    GROUP BY params_id
    ORDER BY n DESC
    LIMIT 50
    """
    res = cli.query(q, parameters={"days": days})
    out: List[Dict[str, Any]] = []
    rows = res.result_rows or []
    for row in rows:
        if isinstance(row, tuple):
            strat = row[0] if len(row) > 0 else "unknown"
            sharpe = float(row[1]) if len(row) > 1 and row[1] is not None else 0.0
            n = int(row[2]) if len(row) > 2 and row[2] is not None else 0
        else:
            strat = row.get("params_id", "unknown")
            try:
                sharpe = float(row.get("sharpe", 0) or 0.0)
                n = int(row.get("n", 0) or 0)
            except Exception:
                sharpe, n = 0.0, 0
        if not strat:
            continue
        confidence = min(1.0, n / 100.0)
        out.append(
            {
                "strategy": strat,
                "sharpe": sharpe,
                "n": n,
                "confidence": confidence,
            }
        )
    return out


def consolidate(days: int = 30) -> int:
    cli = _get_client()
    _ensure_heuristics_table(cli)
    stats = _load_recent_strategy_stats(cli, days=days)
    if not stats:
        log.info("no_heuristics_generated", days=days)
        return 0

    rows = []
    for s in stats:
        condition = f"strategy == '{s['strategy']}'"
        advice = f"{s['strategy']} sharpe {s['sharpe']:.2f} over {days}d (n={s['n']})"
        rows.append(
            {
                "condition": condition,
                "advice": advice,
                "metrics": {"sharpe": float(s["sharpe"]), "sample_size": float(s["n"])},
                "confidence": float(s["confidence"]),
                "status": "ACTIVE",
                "source_experiment_id": "consolidate_knowledge",
                "created_at": datetime.utcnow(),
            }
        )

    cli.insert("trading.knowledge_heuristics", rows)
    log.info("heuristics_written", rows=len(rows))
    return len(rows)


def main():
    try:
        consolidate()
    except Exception as exc:  # pragma: no cover
        log.error("consolidate_knowledge_failed", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
