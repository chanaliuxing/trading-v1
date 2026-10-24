from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.production.recovery_incident_service import RecoveryIncidentService
from services.reconciliation.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_orphan_adoption_is_disabled_in_runtime_reconciler():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-uuid-1",
            "strategy_id": "qqq_intraday_001",
            "state": "NEW",
            "idempotency_key": "idem-x",
        }
    )
    adapter = SimpleNamespace(
        cancel=AsyncMock(return_value=True),
        open_orders=AsyncMock(return_value=[]),
    )

    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    orphan = SimpleNamespace(
        order_id=6,
        perm_id=123,
        symbol="TQQQ",
        side="SELL",
        qty=280.0,
        order_type="LMT",
        status="Submitted",
        order_ref="manual_flatten:abcd",
    )
    unresolved = await rec._adopt_orphans([orphan])
    assert unresolved == [orphan]
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_adoption_does_not_cancel_duplicate_manual_flatten_orphans():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(return_value=None)
    adapter = SimpleNamespace(
        cancel=AsyncMock(return_value=True),
        open_orders=AsyncMock(return_value=[]),
    )

    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    o1 = SimpleNamespace(
        order_id=6,
        perm_id=1,
        symbol="TQQQ",
        side="SELL",
        qty=280.0,
        order_type="LMT",
        status="Submitted",
        order_ref="manual_flatten:dup",
    )
    o2 = SimpleNamespace(
        order_id=18,
        perm_id=2,
        symbol="TQQQ",
        side="SELL",
        qty=280.0,
        order_type="LMT",
        status="Submitted",
        order_ref="manual_flatten:dup",
    )
    unresolved = await rec._adopt_orphans([o1, o2])
    assert unresolved == [o1, o2]
    assert adapter.cancel.await_count == 0


@pytest.mark.asyncio
async def test_orphan_adoption_keeps_manual_flatten_orders_for_different_strategies():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(return_value=None)
    adapter = SimpleNamespace(
        cancel=AsyncMock(return_value=True),
        open_orders=AsyncMock(return_value=[]),
    )

    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    o1 = SimpleNamespace(
        order_id=6,
        perm_id=1,
        symbol="TQQQ",
        side="SELL",
        qty=280.0,
        order_type="LMT",
        status="Submitted",
        order_ref="manual_flatten:intv1:qqq_intraday_001:TQQQ:rth:a1",
    )
    o2 = SimpleNamespace(
        order_id=18,
        perm_id=2,
        symbol="TQQQ",
        side="SELL",
        qty=280.0,
        order_type="LMT",
        status="Submitted",
        order_ref="manual_flatten:intv2:index_super_win_001:TQQQ:rth:a1",
    )
    await rec._adopt_orphans([o1, o2])

    assert adapter.cancel.await_count == 0


@pytest.mark.asyncio
async def test_orphan_adoption_does_not_insert_adopted_rows_when_disabled():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value=None)
    adapter = SimpleNamespace(cancel=AsyncMock(return_value=True))

    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    orphan = SimpleNamespace(
        order_id=4,
        perm_id=1810047544,
        symbol="QQQ",
        side="SELL",
        qty=300.0,
        order_type="LMT",
        status="Submitted",
        order_ref="codex_cleanup:paper_flatten_qqq_300_overnight_aggr",
    )

    unresolved = await rec._adopt_orphans([orphan])
    assert unresolved == [orphan]
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_adoption_terminal_order_ref_case_is_disabled():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-uuid-terminal",
            "strategy_id": "stocks_kernel_shadow_smh_001",
            "state": "CANCELLED",
            "idempotency_key": "idem-terminal",
        }
    )
    adapter = SimpleNamespace(
        cancel=AsyncMock(return_value=True),
        open_orders=AsyncMock(return_value=[]),
    )

    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    orphan = SimpleNamespace(
        order_id=4,
        perm_id=416061160,
        symbol="SOXL",
        side="BUY",
        qty=191.0,
        order_type="LMT",
        status="PreSubmitted",
        order_ref="stkv1:stocks_kernel_shadow_smh_001:SOXL:ENTRY",
    )

    unresolved = await rec._adopt_orphans([orphan])
    assert unresolved == [orphan]
    adapter.cancel.assert_not_awaited()
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_orphan_identity_rejects_unknown_account_safety_ref():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-uuid-1",
            "strategy_id": "nvda_minute_flip_paper_001",
            "symbol": "NVDA",
            "side": "SELL",
            "qty": 1.0,
            "order_type": "STP",
            "state": "SUBMITTED",
            "limit_px": None,
            "stop_px": 1361.11,
        }
    )
    adapter = SimpleNamespace(positions=MagicMock(return_value=[]))
    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    rec._emit_audit = MagicMock()

    synced = await rec.order_quarantine_handler._sync_known_orphan_identity(
        symbol="NVDA",
        broker_order_id="536",
        perm_id=123,
        order_ref="sl:UNKNOWN_ACCOUNT|nvda_minute_flip_paper_001",
        status="PreSubmitted",
        side="SELL",
        qty=1.0,
        order_type="STP",
        stop_px=1361.11,
    )

    assert synced is False
    pg.execute.assert_not_awaited()
    assert rec._emit_audit.call_args.kwargs["reason"] == "unknown_account_safety_order_ref"


@pytest.mark.asyncio
async def test_known_orphan_identity_rejects_bad_price_safety_ref():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-uuid-1",
            "strategy_id": "nvda_minute_flip_paper_001",
            "symbol": "NVDA",
            "side": "SELL",
            "qty": 1.0,
            "order_type": "STP",
            "state": "SUBMITTED",
            "limit_px": None,
            "stop_px": 1361.11,
        }
    )
    position = SimpleNamespace(
        contract=SimpleNamespace(symbol="NVDA"),
        marketPrice=198.0,
        position=1.0,
    )
    adapter = SimpleNamespace(positions=MagicMock(return_value=[position]))
    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, auto_cleanup_ghosts=False)
    rec._emit_audit = MagicMock()

    synced = await rec.order_quarantine_handler._sync_known_orphan_identity(
        symbol="NVDA",
        broker_order_id="536",
        perm_id=123,
        order_ref="sl:nvda_minute_flip_paper_001",
        status="PreSubmitted",
        side="SELL",
        qty=1.0,
        order_type="STP",
        stop_px=1361.11,
    )

    assert synced is False
    pg.execute.assert_not_awaited()
    assert rec._emit_audit.call_args.kwargs["reason"] == "safety_order_price_outside_market_band"


@pytest.mark.asyncio
async def test_runtime_reconciler_ignores_unconfirmed_pending_ib_orders_for_orphans():
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock(), auto_cleanup_ghosts=False)
    shadow = SimpleNamespace(order_id=88, perm_id=0, status="PendingSubmit")
    assert rec._is_unconfirmed_pending_ib_order(shadow) is True
    confirmed = SimpleNamespace(order_id=89, perm_id=123, status="PendingSubmit")
    assert rec._is_unconfirmed_pending_ib_order(confirmed) is False


@pytest.mark.asyncio
async def test_quarantine_order_upserts_on_ib_perm_id_when_available(tmp_path):
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=MagicMock(),
        auto_cleanup_ghosts=False,
        recovery_incident_service=RecoveryIncidentService(artifact_dir=tmp_path / "incidents"),
    )

    await rec._upsert_order_quarantine(
        symbol="QQQ",
        broker_order_id="901",
        perm_id=555,
        ib_client_id=7,
        order_ref="ref-1",
        side="BUY",
        qty=1.0,
        order_type="LMT",
        source="runtime_reconciler",
        reason="orphan",
    )

    sql = pg.execute.await_args.args[0]
    assert "ON CONFLICT (ib_perm_id)" in sql
    assert "ON CONFLICT (symbol, status)" not in sql
    index = rec.recovery_incident_service.write_index(now_et=rec.order_quarantine_handler._now_et())
    assert index["active_count"] == 1
    assert index["incidents"][0]["source"] == "runtime_reconciler"
    assert index["incidents"][0]["active_quarantine"] is True


@pytest.mark.asyncio
async def test_quarantine_order_upserts_on_broker_order_id_when_perm_missing(tmp_path):
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=MagicMock(),
        auto_cleanup_ghosts=False,
        recovery_incident_service=RecoveryIncidentService(artifact_dir=tmp_path / "incidents"),
    )

    await rec._upsert_order_quarantine(
        symbol="QQQ",
        broker_order_id="902",
        perm_id=None,
        ib_client_id=7,
        order_ref="ref-2",
        side="BUY",
        qty=1.0,
        order_type="LMT",
        source="runtime_reconciler",
        reason="orphan",
    )

    sql = pg.execute.await_args.args[0]
    assert "ON CONFLICT (broker_order_id)" in sql


@pytest.mark.asyncio
async def test_quarantine_order_dedupes_synthetic_recovery_action_without_broker_id(tmp_path):
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="UPDATE 1")
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=MagicMock(),
        auto_cleanup_ghosts=False,
        recovery_incident_service=RecoveryIncidentService(artifact_dir=tmp_path / "incidents"),
    )

    await rec._upsert_order_quarantine(
        symbol="NVDA",
        broker_order_id="",
        perm_id=None,
        ib_client_id=None,
        order_ref="",
        side=None,
        qty=1.0,
        order_type=None,
        source="runtime_reconciler_strategy_recovery",
        reason="recovery_action_not_executable",
        payload={
            "strategy_id": "",
            "action_type": "quarantine",
            "target_qty": 0.0,
        },
    )

    assert pg.execute.await_count == 1
    sql = pg.execute.await_args.args[0]
    assert "UPDATE core.core_order_quarantine" in sql
    assert "INSERT INTO core.core_order_quarantine" not in sql
    index = rec.recovery_incident_service.write_index(now_et=rec.order_quarantine_handler._now_et())
    assert index["active_count"] == 1
    assert index["incidents"][0]["strategy_id"] == "runtime_reconciler"
    assert index["incidents"][0]["source"] == "runtime_reconciler_strategy_recovery"


@pytest.mark.asyncio
async def test_resolve_cleared_unknown_quarantine_when_absent_from_broker_and_db(tmp_path):
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "symbol": "QQQ",
                "broker_order_id": "4",
                "perm_id": 953998318,
                "ib_perm_id": 953998318,
                "reason": "unknown_broker_order_detected",
                "status": "active",
                "note": '{"source":"runtime_reconciler_orphan_detected"}',
            }
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "incidents")
    incident_service.upsert_active(
        strategy_id="runtime_reconciler",
        symbol="QQQ",
        qty=1.0,
        reason="unknown_broker_order_detected",
        now_et=datetime.fromisoformat("2026-05-05T21:10:00-04:00"),
        source="runtime_reconciler_orphan_detected",
        active_quarantine=True,
    )
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=MagicMock(),
        auto_cleanup_ghosts=False,
        recovery_incident_service=incident_service,
    )
    rec._emit_audit = MagicMock()

    resolved = await rec._resolve_cleared_order_quarantines(
        db_by_broker_id={},
        db_by_perm_id={},
        ib_by_id={},
        ib_by_perm_id={},
    )

    assert resolved == 1
    assert pg.execute.await_count == 1
    rec._emit_audit.assert_called_once()
    index = incident_service.write_index(now_et=rec.order_quarantine_handler._now_et())
    assert index["active_count"] == 0
    assert index["incidents"][0]["state"] == "verified_flat"


@pytest.mark.asyncio
async def test_resolves_unknown_quarantine_when_broker_and_db_converge(tmp_path):
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "symbol": "SPXS",
                "broker_order_id": "138",
                "perm_id": 1324959371,
                "ib_perm_id": 1324959371,
                "reason": "unknown_broker_order_detected",
                "status": "active",
            }
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "incidents")
    incident_service.upsert_active(
        strategy_id="runtime_reconciler",
        symbol="SPXS",
        qty=1.0,
        reason="unknown_broker_order_detected",
        now_et=datetime.fromisoformat("2026-05-05T21:10:00-04:00"),
        source="runtime_reconciler_orphan_detected",
        active_quarantine=True,
    )
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=MagicMock(),
        auto_cleanup_ghosts=False,
        recovery_incident_service=incident_service,
    )
    rec._emit_audit = MagicMock()

    resolved = await rec._resolve_cleared_order_quarantines(
        db_by_broker_id={138: {"broker_order_id": "138"}},
        db_by_perm_id={"1324959371": {"ib_perm_id": 1324959371}},
        ib_by_id={138: object()},
        ib_by_perm_id={"1324959371": object()},
    )

    assert resolved == 1
    pg.execute.assert_awaited_once()
    assert pg.execute.await_args.args[1] == "resolved"
    rec._emit_audit.assert_called_once()
    assert rec._emit_audit.call_args.kwargs["reason"] == "broker_and_db_converged"
    index = incident_service.write_index(now_et=rec.order_quarantine_handler._now_et())
    assert index["active_count"] == 0


@pytest.mark.asyncio
async def test_keeps_unknown_quarantine_active_when_only_broker_or_db_still_has_order():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "symbol": "SPXS",
                "broker_order_id": "138",
                "perm_id": 1324959371,
                "ib_perm_id": 1324959371,
                "reason": "unknown_broker_order_detected",
                "status": "active",
            }
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    rec = RuntimeReconciler(pg=pg, ib_adapter=MagicMock(), auto_cleanup_ghosts=False)
    rec._emit_audit = MagicMock()

    broker_only = await rec._resolve_cleared_order_quarantines(
        db_by_broker_id={},
        db_by_perm_id={},
        ib_by_id={138: object()},
        ib_by_perm_id={"1324959371": object()},
    )
    db_only = await rec._resolve_cleared_order_quarantines(
        db_by_broker_id={138: {"broker_order_id": "138"}},
        db_by_perm_id={"1324959371": {"ib_perm_id": 1324959371}},
        ib_by_id={},
        ib_by_perm_id={},
    )

    assert broker_only == 0
    assert db_only == 0
    pg.execute.assert_not_awaited()
    rec._emit_audit.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_cleared_position_quarantine_when_broker_ledger_and_orders_flat(tmp_path):
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "symbol": "OPT:QQQ:20260505:682:C",
                "reason": "broker_only_position_requires_manual_intervention",
                "active_count": 3,
            }
        ]
    )
    pg.execute = AsyncMock(return_value="3")
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "incidents")
    incident_service.upsert_active(
        strategy_id="runtime_reconciler",
        symbol="OPT:QQQ:20260505:682:C",
        qty=3.0,
        reason="broker_only_position_requires_manual_intervention",
        now_et=datetime.fromisoformat("2026-05-05T21:10:00-04:00"),
        source="runtime_reconciler_position_mismatch",
        active_quarantine=True,
    )
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=MagicMock(),
        auto_cleanup_ghosts=False,
        recovery_incident_service=incident_service,
    )
    rec._emit_audit = MagicMock()

    resolved = await rec.order_quarantine_handler.resolve_cleared_position_quarantines(
        position_metadata={"ib_positions": {}, "ledger_positions": {}},
        db_orders=[],
        ib_orders=[],
    )

    assert resolved == 3
    assert pg.execute.await_count == 1
    rec._emit_audit.assert_called_once()
    index = incident_service.write_index(now_et=rec.order_quarantine_handler._now_et())
    assert index["active_count"] == 0
    assert index["incidents"][0]["state"] == "verified_flat"


@pytest.mark.asyncio
async def test_keeps_position_quarantine_active_when_ledger_nonflat():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "symbol": "OPT:QQQ:20260505:682:C",
                "reason": "broker_only_position_requires_manual_intervention",
                "active_count": 1,
            }
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    rec = RuntimeReconciler(pg=pg, ib_adapter=MagicMock(), auto_cleanup_ghosts=False)
    rec._emit_audit = MagicMock()

    resolved = await rec.order_quarantine_handler.resolve_cleared_position_quarantines(
        position_metadata={"ib_positions": {}, "ledger_positions": {"OPT:QQQ:20260505:682:C": 2.0}},
        db_orders=[],
        ib_orders=[],
    )

    assert resolved == 0
    pg.execute.assert_not_awaited()
    rec._emit_audit.assert_not_called()


@pytest.mark.asyncio
async def test_quarantine_detected_orphans_syncs_known_order_ref_before_quarantine():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-order-1",
            "strategy_id": "qqq_intraday_001",
            "state": "NEW",
        }
    )
    pg.execute = AsyncMock(return_value="1")
    rec = RuntimeReconciler(pg=pg, ib_adapter=MagicMock(), auto_cleanup_ghosts=False)
    rec._emit_audit = MagicMock()
    orphan = SimpleNamespace(
        order_id=309,
        perm_id=987,
        symbol="TQQQ",
        side="SELL",
        qty=106.0,
        order_type="STP",
        status="Submitted",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:lot-1",
    )

    unresolved = await rec._quarantine_detected_orphans([orphan])

    assert unresolved == []
    assert pg.execute.await_count == 1
    update_sql, update_args = pg.execute.await_args.args[0], pg.execute.await_args.args[1:]
    assert "UPDATE core.core_order" in update_sql
    assert update_args[0] == "309"
    assert update_args[1] == 987
    rec._emit_audit.assert_called_once()


@pytest.mark.asyncio
async def test_quarantine_detected_orphans_syncs_known_protection_registry_ref_before_quarantine():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        side_effect=[
            None,
            {
                "protection_key": "DU123|alpha|NVDA|lot-1|stop_loss|SELL|1|band",
                "account_id": "DU123",
                "strategy_id": "alpha",
                "strategy_instance_id": "alpha-001",
                "symbol": "NVDA",
                "lot_id": "lot-1",
                "side": "SELL",
                "desired_qty": 1.0,
                "desired_price": 95.0,
                "broker_order_id": None,
                "ib_perm_id": None,
                "order_ref": "sl:DU123|alpha-001|NVDA|",
                "state": "SUBMITTED",
                "order_role": "STOP_LOSS",
                "protection_kind": "stop_loss",
                "position_owner_key": "alpha:alpha-001:NVDA:lot-1",
            },
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    position = SimpleNamespace(contract=SimpleNamespace(symbol="NVDA"), marketPrice=100.0, position=1.0)
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=SimpleNamespace(positions=MagicMock(return_value=[position])),
        auto_cleanup_ghosts=False,
    )
    rec._emit_audit = MagicMock()
    orphan = SimpleNamespace(
        order_id=1268,
        perm_id=178525248,
        symbol="NVDA",
        side="SELL",
        qty=1.0,
        order_type="STP",
        status="PreSubmitted",
        order_ref="sl:DU123|alpha-001|NVDA|",
    )

    unresolved = await rec._quarantine_detected_orphans([orphan])

    assert unresolved == []
    assert pg.execute.await_count == 1
    update_sql, update_args = pg.execute.await_args.args[0], pg.execute.await_args.args[1:]
    assert "UPDATE core.core_protection_order_registry" in update_sql
    assert update_args[0] == "1268"
    assert update_args[1] == 178525248
    assert update_args[3] == "DU123|alpha|NVDA|lot-1|stop_loss|SELL|1|band"
    assert rec._emit_audit.call_args.kwargs["reason"] == "known_protection_registry_orphan_converged"


@pytest.mark.asyncio
async def test_quarantine_detected_orphans_rejects_protection_registry_ref_without_owner_lane():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        side_effect=[
            None,
            {
                "protection_key": "DU123|alpha|NVDA||stop_loss|SELL|1|band",
                "account_id": "DU123",
                "strategy_id": "alpha",
                "strategy_instance_id": "",
                "symbol": "NVDA",
                "lot_id": "",
                "side": "SELL",
                "desired_qty": 1.0,
                "desired_price": 95.0,
                "broker_order_id": None,
                "ib_perm_id": None,
                "order_ref": "sl:DU123|alpha|NVDA|",
                "state": "SUBMITTED",
                "order_role": "STOP_LOSS",
                "protection_kind": "stop_loss",
                "position_owner_key": "",
            },
            None,
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    rec = RuntimeReconciler(pg=pg, ib_adapter=MagicMock(), auto_cleanup_ghosts=False)
    rec._emit_audit = MagicMock()
    orphan = SimpleNamespace(
        order_id=1268,
        perm_id=178525248,
        symbol="NVDA",
        side="SELL",
        qty=1.0,
        order_type="STP",
        status="PreSubmitted",
        order_ref="sl:DU123|alpha|NVDA|",
    )

    unresolved = await rec._quarantine_detected_orphans([orphan])

    assert unresolved == [orphan]
    quarantine_sql = pg.execute.await_args.args[0]
    assert "core.core_order_quarantine" in quarantine_sql
    assert any(
        call.kwargs.get("reason") == "incomplete_protection_owner_identity"
        for call in rec._emit_audit.call_args_list
    )


@pytest.mark.asyncio
async def test_quarantine_detected_orphans_does_not_sync_terminal_order_ref():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        side_effect=[
            {
                "order_id": "db-order-terminal",
                "strategy_id": "qqq_intraday_001",
                "state": "CANCELLED",
            },
            None,
        ]
    )
    pg.execute = AsyncMock(return_value="1")
    rec = RuntimeReconciler(pg=pg, ib_adapter=MagicMock(), auto_cleanup_ghosts=False)
    orphan = SimpleNamespace(
        order_id=310,
        perm_id=988,
        symbol="TQQQ",
        side="SELL",
        qty=106.0,
        order_type="STP",
        status="Submitted",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:terminal",
    )

    unresolved = await rec._quarantine_detected_orphans([orphan])

    assert unresolved == [orphan]
    sql = pg.execute.await_args.args[0]
    assert "core.core_order_quarantine" in sql
