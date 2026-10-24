from unittest.mock import AsyncMock, MagicMock

import pytest

from services.ibkr.ib_order_truth_schema import assert_ib_order_truth_schema


@pytest.mark.asyncio
async def test_assert_ib_order_truth_schema_accepts_identity_indexes():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {"table_name": "core_order", "column_name": "ib_client_id"},
                {"table_name": "core_order", "column_name": "ib_session_role"},
                {"table_name": "core_order", "column_name": "ib_connection_epoch"},
                {"table_name": "core_order_quarantine", "column_name": "symbol"},
                {"table_name": "core_order_quarantine", "column_name": "broker_order_id"},
                {"table_name": "core_order_quarantine", "column_name": "perm_id"},
                {"table_name": "core_order_quarantine", "column_name": "ib_perm_id"},
                {"table_name": "core_order_quarantine", "column_name": "ib_client_id"},
                {"table_name": "core_order_quarantine", "column_name": "order_ref"},
                {"table_name": "core_order_quarantine", "column_name": "reason"},
                {"table_name": "core_order_quarantine", "column_name": "status"},
                {"table_name": "core_order_quarantine", "column_name": "note"},
                {"table_name": "core_order_quarantine", "column_name": "observed_at"},
                {"table_name": "core_order_quarantine", "column_name": "created_at"},
                {"table_name": "core_order_quarantine", "column_name": "updated_at"},
                {"table_name": "core_order_quarantine", "column_name": "resolved_at"},
            ],
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
        ]
    )

    await assert_ib_order_truth_schema(pg, source="test")


@pytest.mark.asyncio
async def test_assert_ib_order_truth_schema_rejects_deprecated_symbol_unique_index():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {"table_name": "core_order", "column_name": "ib_client_id"},
                {"table_name": "core_order", "column_name": "ib_session_role"},
                {"table_name": "core_order", "column_name": "ib_connection_epoch"},
                {"table_name": "core_order_quarantine", "column_name": "symbol"},
                {"table_name": "core_order_quarantine", "column_name": "broker_order_id"},
                {"table_name": "core_order_quarantine", "column_name": "perm_id"},
                {"table_name": "core_order_quarantine", "column_name": "ib_perm_id"},
                {"table_name": "core_order_quarantine", "column_name": "ib_client_id"},
                {"table_name": "core_order_quarantine", "column_name": "order_ref"},
                {"table_name": "core_order_quarantine", "column_name": "reason"},
                {"table_name": "core_order_quarantine", "column_name": "status"},
                {"table_name": "core_order_quarantine", "column_name": "note"},
                {"table_name": "core_order_quarantine", "column_name": "observed_at"},
                {"table_name": "core_order_quarantine", "column_name": "created_at"},
                {"table_name": "core_order_quarantine", "column_name": "updated_at"},
                {"table_name": "core_order_quarantine", "column_name": "resolved_at"},
            ],
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
                {"indexname": "core_order_quarantine_active_symbol_uk"},
            ],
        ]
    )

    with pytest.raises(RuntimeError, match="ib_order_truth_schema_invalid"):
        await assert_ib_order_truth_schema(pg, source="test")
