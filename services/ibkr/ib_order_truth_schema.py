from __future__ import annotations

from typing import Any, Dict, Iterable, Set, Tuple

from prometheus_client import Counter

from libs.logger import get_logger

logger = get_logger(__name__)
ib_order_truth_schema_validation_total = Counter(
    "ib_order_truth_schema_validation_total",
    "Runtime schema validation result for IB order truth tables",
    ["result", "source"],
)

_EXPECTED_COLUMNS: Dict[str, Set[str]] = {
    "core_order": {
        "ib_client_id",
        "ib_session_role",
        "ib_connection_epoch",
    },
    "core_order_quarantine": {
        "symbol",
        "broker_order_id",
        "perm_id",
        "ib_perm_id",
        "ib_client_id",
        "order_ref",
        "reason",
        "status",
        "note",
        "observed_at",
        "created_at",
        "updated_at",
        "resolved_at",
    },
}

_EXPECTED_INDEXES: Set[str] = {
    "core_order_quarantine_symbol_status_idx",
    "core_order_quarantine_active_broker_order_id_uk",
    "core_order_quarantine_active_ib_perm_id_uk",
}

_DEPRECATED_INDEXES: Set[str] = {
    "core_order_quarantine_active_symbol_uk",
}


def _normalize_column_rows(rows: Iterable[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for row in rows or []:
        table_name = str((row or {}).get("table_name") or "").strip()
        column_name = str((row or {}).get("column_name") or "").strip()
        if table_name and column_name:
            out.add((table_name, column_name))
    return out


def _normalize_index_rows(rows: Iterable[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for row in rows or []:
        name = str((row or {}).get("indexname") or "").strip()
        if name:
            out.add(name)
    return out


async def assert_ib_order_truth_schema(pg, *, source: str) -> None:
    column_rows = await pg.fetchall(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'core'
          AND table_name IN ('core_order', 'core_order_quarantine')
        """
    )
    available_columns = _normalize_column_rows(column_rows)
    missing_columns = []
    for table_name, expected in _EXPECTED_COLUMNS.items():
        for column_name in sorted(expected):
            if (table_name, column_name) not in available_columns:
                missing_columns.append(f"{table_name}.{column_name}")

    index_rows = await pg.fetchall(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'core'
          AND tablename = 'core_order_quarantine'
        """
    )
    available_indexes = _normalize_index_rows(index_rows)
    missing_indexes = sorted(_EXPECTED_INDEXES - available_indexes)
    deprecated_indexes = sorted(_DEPRECATED_INDEXES & available_indexes)

    if missing_columns or missing_indexes or deprecated_indexes:
        ib_order_truth_schema_validation_total.labels(result="invalid", source=source).inc()
        logger.error(
            "ib_order_truth_schema_invalid",
            source=source,
            missing_columns=missing_columns,
            missing_indexes=missing_indexes,
            deprecated_indexes=deprecated_indexes,
        )
        raise RuntimeError(
            "ib_order_truth_schema_invalid:"
            f" missing_columns={missing_columns}"
            f" missing_indexes={missing_indexes}"
            f" deprecated_indexes={deprecated_indexes}"
        )

    ib_order_truth_schema_validation_total.labels(result="ok", source=source).inc()
    logger.info("ib_order_truth_schema_verified", source=source)
