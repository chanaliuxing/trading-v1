from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


FILL_ATTRIBUTION_ACTIVE_STATUSES = ("unknown", "ambiguous", "manual_required")
FILL_ATTRIBUTION_REASONS = ("unattributed_broker_fill", "unknown_broker_fill", "ambiguous_broker_fill")
FILL_ATTRIBUTION_ADJUDICATION_DECISIONS = ("manual_resolved", "manual_required", "ignored")


async def ensure_fill_attribution_ledger_schema(*, pg: Any) -> None:
    await pg.execute(
        """
        CREATE TABLE IF NOT EXISTS core.fill_attribution_ledger (
            attribution_id uuid DEFAULT gen_random_uuid() NOT NULL,
            event_key text NOT NULL,
            account text NULL,
            symbol text NOT NULL,
            asset_class text DEFAULT 'STK'::text NOT NULL,
            broker_order_id text NULL,
            perm_id int8 NULL,
            order_ref text NULL,
            exec_id text NULL,
            side text NULL,
            qty numeric NULL,
            price numeric NULL,
            event_ts timestamptz NULL,
            attribution_status text NOT NULL,
            attribution_reason text NOT NULL,
            matched_core_order_id uuid NULL,
            matched_strategy_id text NULL,
            candidate_count int4 DEFAULT 0 NOT NULL,
            candidate_snapshot jsonb DEFAULT '[]'::jsonb NOT NULL,
            quarantine_id uuid NULL,
            pnl_eligible boolean DEFAULT false NOT NULL,
            ledger_eligible boolean DEFAULT false NOT NULL,
            manual_adjudication_required boolean DEFAULT true NOT NULL,
            adjudicated_by text NULL,
            adjudicated_at timestamptz NULL,
            adjudication_note text NULL,
            created_at timestamptz DEFAULT now() NOT NULL,
            updated_at timestamptz DEFAULT now() NOT NULL,
            CONSTRAINT fill_attribution_ledger_pkey PRIMARY KEY (attribution_id),
            CONSTRAINT fill_attribution_ledger_event_key_uk UNIQUE (event_key),
            CONSTRAINT fill_attribution_ledger_status_check CHECK (
                attribution_status = ANY (ARRAY[
                    'unknown'::text,
                    'ambiguous'::text,
                    'verified'::text,
                    'manual_required'::text,
                    'manual_resolved'::text,
                    'ignored'::text
                ])
            )
        )
        """
    )
    await pg.execute(
        """
        CREATE INDEX IF NOT EXISTS fill_attribution_ledger_status_symbol_idx
        ON core.fill_attribution_ledger (attribution_status, symbol, updated_at DESC)
        """
    )
    await pg.execute(
        """
        CREATE INDEX IF NOT EXISTS fill_attribution_ledger_identity_idx
        ON core.fill_attribution_ledger (account, symbol, broker_order_id, perm_id, order_ref, exec_id)
        """
    )
    await pg.execute(
        """
        CREATE INDEX IF NOT EXISTS fill_attribution_ledger_adjudication_idx
        ON core.fill_attribution_ledger (manual_adjudication_required, adjudicated_at DESC, updated_at DESC)
        """
    )


async def adjudicate_fill_attribution(
    *,
    pg: Any,
    event_key: str,
    decision: str,
    adjudicated_by: str,
    matched_core_order_id: str | None = None,
    matched_strategy_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a bounded manual adjudication decision for one fill attribution row."""
    await ensure_fill_attribution_ledger_schema(pg=pg)
    event_key_u = str(event_key or "").strip()
    if not event_key_u:
        raise ValueError("event_key is required")
    decision_u = str(decision or "").strip().lower()
    if decision_u not in FILL_ATTRIBUTION_ADJUDICATION_DECISIONS:
        raise ValueError(f"unsupported fill attribution decision: {decision}")
    actor = str(adjudicated_by or "").strip()
    if not actor:
        raise ValueError("adjudicated_by is required")
    matched_order = str(matched_core_order_id or "").strip()
    matched_strategy = str(matched_strategy_id or "").strip()
    if decision_u == "manual_resolved" and (not matched_order or not matched_strategy):
        raise ValueError("manual_resolved requires matched_core_order_id and matched_strategy_id")
    pnl_eligible = decision_u == "manual_resolved"
    ledger_eligible = decision_u == "manual_resolved"
    manual_required = decision_u == "manual_required"
    row = await pg.fetchone(
        """
        UPDATE core.fill_attribution_ledger
        SET attribution_status = $2,
            attribution_reason = CASE
                WHEN $2 = 'manual_resolved' THEN 'manual_adjudicated_verified'
                WHEN $2 = 'manual_required' THEN 'manual_adjudication_required'
                ELSE 'manual_adjudicated_ignored'
            END,
            matched_core_order_id = CASE
                WHEN NULLIF($3, '') IS NULL THEN matched_core_order_id
                ELSE $3::uuid
            END,
            matched_strategy_id = COALESCE(NULLIF($4, ''), matched_strategy_id),
            pnl_eligible = $5,
            ledger_eligible = $6,
            manual_adjudication_required = $7,
            adjudicated_by = $8,
            adjudicated_at = now(),
            adjudication_note = NULLIF($9, ''),
            updated_at = now()
        WHERE event_key = $1
        RETURNING event_key,
                  symbol,
                  attribution_status,
                  attribution_reason,
                  matched_core_order_id::text AS matched_core_order_id,
                  matched_strategy_id,
                  pnl_eligible,
                  ledger_eligible,
                  manual_adjudication_required,
                  adjudicated_by,
                  adjudicated_at,
                  adjudication_note
        """,
        event_key_u,
        decision_u,
        matched_order,
        matched_strategy,
        bool(pnl_eligible),
        bool(ledger_eligible),
        bool(manual_required),
        actor,
        str(note or "").strip(),
    )
    if not row:
        raise LookupError(f"fill attribution event not found: {event_key_u}")
    return dict(row)


async def _fill_attribution_ledger_exists(pg: Any) -> bool:
    query = """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'core'
              AND table_name = 'fill_attribution_ledger'
        ) AS exists
        """
    fetchval = getattr(pg, "fetchval", None)
    if fetchval is not None:
        return bool(await fetchval(query))
    fetchone = getattr(pg, "fetchone", None)
    if fetchone is not None:
        row = await fetchone(query)
        return bool((row or {}).get("exists")) if hasattr(row, "get") else False
    return False


async def query_fill_attribution_readiness(
    *,
    pg: Any,
    generated_at: datetime,
    strategy_ids: list[str],
    symbols: list[str],
) -> dict[str, Any]:
    if getattr(pg, "execute", None) is not None:
        await ensure_fill_attribution_ledger_schema(pg=pg)
    ledger_present = await _fill_attribution_ledger_exists(pg)
    if ledger_present:
        unknown_rows = await pg.fetchall(
            """
            SELECT symbol,
                   attribution_status AS reason,
                   COUNT(*)::int AS count,
                   SUM(COALESCE(candidate_count, 0))::int AS candidate_count,
                   BOOL_OR(manual_adjudication_required) AS manual_adjudication_required
            FROM core.fill_attribution_ledger
            WHERE attribution_status = ANY($1::text[])
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
            GROUP BY symbol, attribution_status
            ORDER BY count DESC, symbol, attribution_status
            LIMIT 100
            """,
            list(FILL_ATTRIBUTION_ACTIVE_STATUSES),
            symbols or None,
        )
        summary_rows = await pg.fetchall(
            """
            SELECT attribution_status AS status,
                   COUNT(*)::int AS count,
                   COUNT(*) FILTER (WHERE pnl_eligible)::int AS pnl_eligible_count,
                   COUNT(*) FILTER (WHERE ledger_eligible)::int AS ledger_eligible_count,
                   COUNT(*) FILTER (WHERE manual_adjudication_required)::int AS manual_required_count
            FROM core.fill_attribution_ledger
            WHERE event_ts >= date_trunc('day', $1::timestamptz)
              AND event_ts < date_trunc('day', $1::timestamptz) + interval '1 day'
              AND ($2::text[] IS NULL OR symbol = ANY($2::text[]))
            GROUP BY attribution_status
            ORDER BY attribution_status
            """,
            generated_at,
            symbols or None,
        )
        source_table = "core.fill_attribution_ledger"
    else:
        unknown_rows = await pg.fetchall(
            """
            SELECT symbol,
                   reason,
                   COUNT(*)::int AS count,
                   0::int AS candidate_count,
                   true AS manual_adjudication_required
            FROM core.core_order_quarantine
            WHERE status = 'active'
              AND reason IN ('unattributed_broker_fill', 'unknown_broker_fill', 'ambiguous_broker_fill')
              AND ($1::text[] IS NULL OR symbol = ANY($1::text[]))
            GROUP BY symbol, reason
            ORDER BY count DESC, symbol, reason
            LIMIT 100
            """,
            symbols or None,
        )
        summary_rows = []
        source_table = "core.core_order_quarantine"
    legacy_rows = await pg.fetchall(
        """
        SELECT symbol,
               reason,
               COUNT(*)::int AS count
        FROM core.core_order_quarantine
        WHERE status = 'active'
          AND reason IN ('unattributed_broker_fill', 'unknown_broker_fill', 'ambiguous_broker_fill')
          AND ($1::text[] IS NULL OR symbol = ANY($1::text[]))
        GROUP BY symbol, reason
        ORDER BY count DESC, symbol, reason
        LIMIT 100
        """,
        symbols or None,
    )
    unknown_fills = [dict(row) for row in unknown_rows or []]
    legacy_unknown_fills = [dict(row) for row in legacy_rows or []]
    ledger_unknown_count = sum(int(row.get("count") or 0) for row in unknown_fills)
    legacy_unknown_count = sum(int(row.get("count") or 0) for row in legacy_unknown_fills)
    unknown_fill_count = max(ledger_unknown_count, legacy_unknown_count)
    ambiguous_fill_count = max(
        sum(
            int(row.get("count") or 0)
            for row in unknown_fills
            if str(row.get("reason") or "") in {"ambiguous", "ambiguous_broker_fill"}
        ),
        sum(
            int(row.get("count") or 0)
            for row in legacy_unknown_fills
            if str(row.get("reason") or "") == "ambiguous_broker_fill"
        ),
    )
    clean = unknown_fill_count <= 0
    daily_stats = _daily_stats(summary_rows if ledger_present else [], unknown_fills=unknown_fills)
    return {
        "schema_version": 2,
        "artifact_type": "fill_attribution",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "go_no_go": "go" if clean else "no_go",
        "enabled": True,
        "lookup_failed": False,
        "fill_truth_clean": clean,
        "source_table": source_table,
        "ledger_present": ledger_present,
        "ledger_unknown_fill_count": ledger_unknown_count,
        "legacy_unknown_fill_count": legacy_unknown_count,
        "unknown_fill_count": unknown_fill_count,
        "ambiguous_fill_count": ambiguous_fill_count,
        "unknown_fills": unknown_fills,
        "legacy_unknown_fills": legacy_unknown_fills,
        "daily_stats": daily_stats,
        "blockers": [] if clean else ["unknown_or_ambiguous_broker_fills_active"],
        "clear_conditions": [] if clean else ["adjudicate_or_resolve_fill_attribution_ledger"],
    }


def _daily_stats(rows: list[Any], *, unknown_fills: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, dict[str, int]] = {}
    for row in rows or []:
        item = dict(row)
        status = str(item.get("status") or "unknown").strip().lower() or "unknown"
        by_status[status] = {
            "count": int(item.get("count") or 0),
            "pnl_eligible_count": int(item.get("pnl_eligible_count") or 0),
            "ledger_eligible_count": int(item.get("ledger_eligible_count") or 0),
            "manual_required_count": int(item.get("manual_required_count") or 0),
        }
    if not by_status and unknown_fills:
        for row in unknown_fills:
            status = str(row.get("reason") or "unknown").strip().lower() or "unknown"
            bucket = by_status.setdefault(
                status,
                {"count": 0, "pnl_eligible_count": 0, "ledger_eligible_count": 0, "manual_required_count": 0},
            )
            bucket["count"] += int(row.get("count") or 0)
            bucket["manual_required_count"] += int(row.get("count") or 0)
    total = sum(item["count"] for item in by_status.values())
    return {
        "total_count": total,
        "by_status": by_status,
        "verified_count": int(by_status.get("verified", {}).get("count", 0)),
        "manual_resolved_count": int(by_status.get("manual_resolved", {}).get("count", 0)),
        "ignored_count": int(by_status.get("ignored", {}).get("count", 0)),
        "unknown_count": int(by_status.get("unknown", {}).get("count", 0)),
        "ambiguous_count": int(by_status.get("ambiguous", {}).get("count", 0)),
        "manual_required_count": sum(item["manual_required_count"] for item in by_status.values()),
        "pnl_eligible_count": sum(item["pnl_eligible_count"] for item in by_status.values()),
        "ledger_eligible_count": sum(item["ledger_eligible_count"] for item in by_status.values()),
    }


def disabled_fill_attribution_status(*, generated_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "fill_attribution",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "go_no_go": "go",
        "enabled": False,
        "lookup_failed": False,
        "fill_truth_clean": True,
        "unknown_fill_count": 0,
        "ambiguous_fill_count": 0,
        "unknown_fills": [],
        "blockers": [],
        "clear_conditions": [],
    }


def failed_fill_attribution_status(*, generated_at: datetime, error: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "fill_attribution",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "go_no_go": "no_go",
        "enabled": True,
        "lookup_failed": True,
        "fill_truth_clean": False,
        "unknown_fill_count": 0,
        "ambiguous_fill_count": 0,
        "unknown_fills": [],
        "blockers": ["fill_attribution_lookup_failed"],
        "clear_conditions": ["restore_fill_attribution_quarantine_lookup"],
        "error": error,
    }


def write_fill_attribution_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/fill_attribution/fill_attribution_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
