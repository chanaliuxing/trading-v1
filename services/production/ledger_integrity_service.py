from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


async def query_position_ledger_integrity(
    *,
    pg: Any,
    strategy_ids: list[str],
    symbols: list[str],
    generated_at: datetime,
    notional_residual_threshold: float | None = None,
) -> dict[str, Any]:
    threshold = (
        float(notional_residual_threshold)
        if notional_residual_threshold is not None
        else float(os.getenv("POSITION_LEDGER_ZERO_QTY_NOTIONAL_RESIDUAL_USD", "1.0"))
    )
    rows = await pg.fetchall(
        """
        WITH ledger_net AS (
            SELECT COALESCE(account_id, '') AS account_id,
                   strategy_id,
                   symbol,
                   COALESCE(lot_id, '') AS lot_id,
                   SUM(qty_delta)::float AS qty
            FROM core.position_ledger_event
            WHERE ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
            GROUP BY COALESCE(account_id, ''), strategy_id, symbol, COALESCE(lot_id, '')
            HAVING abs(SUM(qty_delta)) > 1e-9
        ),
        symbol_net AS (
            SELECT account_id,
                   strategy_id,
                   symbol,
                   SUM(qty)::float AS net_qty
            FROM ledger_net
            GROUP BY account_id, strategy_id, symbol
        )
        SELECT ledger_net.*, symbol_net.net_qty
        FROM ledger_net
        JOIN symbol_net
          ON symbol_net.account_id = ledger_net.account_id
         AND symbol_net.strategy_id = ledger_net.strategy_id
         AND symbol_net.symbol = ledger_net.symbol
        WHERE (ledger_net.account_id = '' OR ledger_net.account_id = 'UNKNOWN_ACCOUNT' OR ledger_net.lot_id = '')
        ORDER BY ledger_net.strategy_id, ledger_net.symbol, ledger_net.account_id, ledger_net.lot_id
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
    )
    divergence_rows = await pg.fetchall(
        """
        WITH ledger_net AS (
            SELECT COALESCE(account_id, '') AS account_id,
                   strategy_id,
                   symbol,
                   SUM(qty_delta)::float AS qty
            FROM core.position_ledger_event
            WHERE ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
            GROUP BY COALESCE(account_id, ''), strategy_id, symbol
            HAVING abs(SUM(qty_delta)) > 1e-9
        )
        SELECT strategy_id,
               symbol,
               COUNT(DISTINCT account_id)::int AS account_count,
               array_agg(account_id ORDER BY account_id) AS accounts
        FROM ledger_net
        GROUP BY strategy_id, symbol
        HAVING COUNT(DISTINCT account_id) > 1
        ORDER BY strategy_id, symbol
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
    )
    notional_residual_rows = await pg.fetchall(
        """
        SELECT COALESCE(account_id, '') AS account_id,
               strategy_id,
               symbol,
               COALESCE(lot_id, '') AS lot_id,
               qty::float AS qty,
               notional_sum::float AS notional_sum,
               abs(notional_sum)::float AS abs_notional_sum
        FROM core.position_ledger_current
        WHERE ($1::text[] IS NULL OR strategy_id = ANY($1::text[]))
          AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
          AND abs(qty) <= 1e-9
          AND abs(COALESCE(notional_sum, 0)) > $3::float
        ORDER BY abs(COALESCE(notional_sum, 0)) DESC, strategy_id, symbol, account_id, lot_id
        LIMIT 100
        """,
        strategy_ids or None,
        symbols or None,
        threshold,
    )
    issues = [dict(row) for row in rows or []]
    divergences = [dict(row) for row in divergence_rows or []]
    notional_residuals = [dict(row) for row in notional_residual_rows or []]
    dirty = bool(issues or divergences or notional_residuals)
    return {
        "schema_version": 1,
        "artifact_type": "ledger_integrity",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "go_no_go": "no_go" if dirty else "go",
        "enabled": True,
        "lookup_failed": False,
        "position_ledger_clean": not dirty,
        "lot_truth_clean": not bool(issues),
        "account_truth_clean": not bool(divergences),
        "notional_truth_clean": not bool(notional_residuals),
        "dirty_symbol_count": len({str(item.get("symbol") or "") for item in issues + divergences + notional_residuals}),
        "issue_count": len(issues),
        "account_divergence_count": len(divergences),
        "zero_qty_notional_residual_count": len(notional_residuals),
        "zero_qty_notional_residual_threshold": threshold,
        "issues": issues,
        "account_divergences": divergences,
        "zero_qty_notional_residuals": notional_residuals,
        "blockers": _blockers(
            issue_count=len(issues),
            account_divergence_count=len(divergences),
            zero_qty_notional_residual_count=len(notional_residuals),
        ),
        "clear_conditions": [
            "resolve_unknown_account_and_null_lot_nonzero_qty",
            "resolve_mixed_account_symbol_positions",
            "clear_zero_qty_nonzero_notional_residuals",
        ],
    }


def disabled_ledger_integrity_status(*, generated_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "ledger_integrity",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "go_no_go": "go",
        "enabled": False,
        "lookup_failed": False,
        "position_ledger_clean": True,
        "lot_truth_clean": True,
        "account_truth_clean": True,
        "notional_truth_clean": True,
        "issue_count": 0,
        "account_divergence_count": 0,
        "zero_qty_notional_residual_count": 0,
        "issues": [],
        "account_divergences": [],
        "zero_qty_notional_residuals": [],
        "blockers": [],
        "clear_conditions": [],
    }


def failed_ledger_integrity_status(*, generated_at: datetime, error: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "ledger_integrity",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "go_no_go": "no_go",
        "enabled": True,
        "lookup_failed": True,
        "position_ledger_clean": False,
        "lot_truth_clean": False,
        "account_truth_clean": False,
        "notional_truth_clean": False,
        "issue_count": 0,
        "account_divergence_count": 0,
        "zero_qty_notional_residual_count": 0,
        "issues": [],
        "account_divergences": [],
        "zero_qty_notional_residuals": [],
        "blockers": ["position_ledger_integrity_lookup_failed"],
        "clear_conditions": ["restore_postgres_ledger_integrity_lookup"],
        "error": error,
    }


def write_ledger_integrity_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/ledger_integrity/ledger_integrity_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _blockers(
    *,
    issue_count: int,
    account_divergence_count: int,
    zero_qty_notional_residual_count: int,
) -> list[str]:
    blockers: list[str] = []
    if issue_count > 0:
        blockers.append("position_ledger_unknown_account_or_null_lot")
    if account_divergence_count > 0:
        blockers.append("position_ledger_account_divergence")
    if zero_qty_notional_residual_count > 0:
        blockers.append("position_ledger_zero_qty_notional_residual")
    return blockers
