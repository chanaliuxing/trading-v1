from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta

import pytest

from services.reconciliation.contracts import ReconcilerComponentResult
from services.reconciliation.broker_truth_convergence import ConvergenceResult
from services.reconciliation.runtime_reconciler import RuntimeReconciler, ReconciliationReport
from services.health import SystemHealthController
from services.production.recovery_readiness import get_recovery_readiness_service


def _schema_rows():
    return [
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
    ]


class _IBOrder:
    order_id = 22
    perm_id = 123
    symbol = "QQQ"
    status = "Submitted"
    order_ref = "startup-orphan"


@pytest.mark.asyncio
async def test_runtime_reconciler_blocks_startup_on_unresolved_orphans():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            _schema_rows(),
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
        ]
    )
    adapter = MagicMock()
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        startup_block_on_orphans=True,
        startup_block_on_ghosts=True,
    )
    rec._fetch_db_open_orders = AsyncMock(return_value=[])
    rec._fetch_ib_open_orders = AsyncMock(return_value=[_IBOrder()])
    rec.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=True,
            reason="position_ledger_clean",
            metadata={"mismatch_count": 0},
        )
    )

    with pytest.raises(RuntimeError, match="orphans_found"):
        await rec.ensure_startup_ready()


@pytest.mark.asyncio
async def test_runtime_reconciler_startup_gate_passes_when_report_clean():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            _schema_rows(),
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
        ]
    )
    adapter = MagicMock()
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        startup_block_on_orphans=True,
        startup_block_on_ghosts=True,
    )
    rec._fetch_db_open_orders = AsyncMock(return_value=[])
    rec._fetch_ib_open_orders = AsyncMock(return_value=[])
    rec.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=True,
            reason="position_ledger_clean",
            metadata={"mismatch_count": 0},
        )
    )

    await rec.ensure_startup_ready()


@pytest.mark.asyncio
async def test_runtime_reconciler_blocks_startup_on_position_mismatch_by_default():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            _schema_rows(),
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
        ]
    )
    adapter = MagicMock()
    rec = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        startup_block_on_orphans=True,
        startup_block_on_ghosts=True,
    )
    rec._fetch_db_open_orders = AsyncMock(return_value=[])
    rec._fetch_ib_open_orders = AsyncMock(return_value=[])
    rec.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=False,
            reason="position_ledger_diverged",
            metadata={"mismatch_count": 1},
        )
    )

    with pytest.raises(RuntimeError, match="position_mismatches_found"):
        await rec.ensure_startup_ready()


@pytest.mark.asyncio
async def test_runtime_reconciler_returns_clean_startup_result():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            _schema_rows(),
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
        ]
    )
    adapter = MagicMock()
    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    rec._fetch_db_open_orders = AsyncMock(return_value=[])
    rec._fetch_ib_open_orders = AsyncMock(return_value=[])
    rec.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=True,
            reason="position_ledger_clean",
            metadata={"mismatch_count": 0},
        )
    )

    result = await rec.ensure_startup_ready()

    assert result.ok is True
    assert rec.last_startup_result is result


@pytest.mark.asyncio
async def test_runtime_reconciler_converges_broker_flat_position_mismatch():
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec.broker_truth_convergence.converge_position_mismatch = AsyncMock(
        return_value=ConvergenceResult(
            ok=True,
            reason="position_mismatch_adjusted",
            metadata={"symbol": "TQQQ", "diff": -106.0},
        )
    )
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [
                {
                    "symbol": "TQQQ",
                    "ib_qty": 0.0,
                    "ledger_qty": 106.0,
                    "diff": -106.0,
                    "strategy_id": "qqq_intraday_001",
                }
            ],
        },
    )

    await rec._converge_broker_flat_position_mismatches(position_result=result)

    rec.broker_truth_convergence.converge_position_mismatch.assert_awaited_once_with(
        symbol="TQQQ",
        ib_qty=0.0,
        ledger_qty=106.0,
        strategy_id="qqq_intraday_001",
        reason="runtime_reconciler_broker_flat_position_convergence",
    )
    rec._emit_audit.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_reconciler_converges_broker_flat_positions_before_startup_gate():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            _schema_rows(),
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
            _schema_rows(),
            [
                {"indexname": "core_order_quarantine_symbol_status_idx"},
                {"indexname": "core_order_quarantine_active_broker_order_id_uk"},
                {"indexname": "core_order_quarantine_active_ib_perm_id_uk"},
            ],
        ]
    )
    adapter = MagicMock()
    rec = RuntimeReconciler(pg=pg, ib_adapter=adapter, startup_block_on_mismatches=True)
    rec._fetch_db_open_orders = AsyncMock(return_value=[])
    rec._fetch_ib_open_orders = AsyncMock(return_value=[])
    rec.broker_truth_convergence.converge_position_mismatch = AsyncMock(
        return_value=ConvergenceResult(
            ok=True,
            reason="position_mismatch_adjusted",
            metadata={"symbol": "TQQQ", "diff": -106.0},
        )
    )
    rec.position_reconciler.reconcile_once = AsyncMock(
        side_effect=[
            ReconcilerComponentResult(
                component="position",
                ok=False,
                reason="position_ledger_diverged",
                metadata={
                    "mismatch_count": 1,
                    "mismatches": [
                        {
                            "symbol": "TQQQ",
                            "ib_qty": 0.0,
                            "ledger_qty": 106.0,
                            "diff": -106.0,
                            "strategy_id": "qqq_intraday_001",
                        }
                    ],
                },
            ),
            ReconcilerComponentResult(
                component="position",
                ok=True,
                reason="position_ledger_clean",
                metadata={"mismatch_count": 0, "mismatches": []},
            ),
        ]
    )

    result = await rec.ensure_startup_ready()

    assert result.ok is True
    assert result.metadata["position_mismatches"] == 0
    rec.broker_truth_convergence.converge_position_mismatch.assert_awaited_once_with(
        symbol="TQQQ",
        ib_qty=0.0,
        ledger_qty=106.0,
        strategy_id="qqq_intraday_001",
        reason="runtime_reconciler_broker_flat_position_convergence",
    )


@pytest.mark.asyncio
async def test_runtime_reconciler_publishes_system_degraded_on_runtime_anomaly():
    controller = MagicMock()
    controller.publish = AsyncMock(return_value=None)
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock(), system_health_controller=controller)

    await rec._publish_runtime_health_if_needed(
        ghosts=0,
        unresolved_orphans=1,
        mismatches=0,
        position_mismatches=1,
        active_quarantine_count=1,
    )

    controller.publish.assert_awaited_once()
    kwargs = controller.publish.await_args.kwargs
    assert kwargs["event_type"] == "system_degraded"
    assert kwargs["reason"] == "runtime_reconciliation_anomaly_detected"
    assert kwargs["metadata"]["order_truth_clean"] is False
    assert kwargs["metadata"]["position_ledger_clean"] is False


@pytest.mark.asyncio
async def test_runtime_reconciler_clean_runtime_health_resumes_after_broker_api_ready():
    controller = SystemHealthController()
    await controller.on_ib_connection_event(
        {
            "event_type": "broker_api_ready",
            "reason": "ib_runtime_recovery_api_ready",
            "source": "ib_connection_manager.runtime_recovery",
            "resume_ready": False,
            "broker_connected": True,
            "broker_truth_complete": False,
            "order_truth_clean": False,
            "position_ledger_clean": False,
            "strategies_paused_ack": True,
        }
    )
    assert controller.state == "recovering"
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock(), system_health_controller=controller)

    await rec._publish_runtime_health_if_needed(
        ghosts=0,
        unresolved_orphans=0,
        mismatches=0,
        position_mismatches=0,
        active_quarantine_count=0,
    )

    assert controller.state == "running"
    assert controller.last_event["event_type"] == "system_resumed"
    assert controller.last_event["reason"] == "runtime_reconciliation_clean"
    metadata = controller.last_event["metadata"]
    assert metadata["order_truth_clean"] is True
    assert metadata["position_ledger_clean"] is True
    assert metadata["active_quarantine_count"] == 0


@pytest.mark.asyncio
async def test_runtime_reconciler_clean_truth_waits_for_platform_recovery_readiness():
    controller = SystemHealthController()
    await controller.on_ib_connection_event(
        {
            "event_type": "broker_api_ready",
            "reason": "ib_runtime_recovery_api_ready",
            "source": "ib_connection_manager.runtime_recovery",
            "requires_runtime_reconciler": True,
        }
    )
    rec = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        system_health_controller=controller,
        recovery_required_components=["market_data_service"],
    )

    await rec._publish_runtime_health_if_needed(
        ghosts=0,
        unresolved_orphans=0,
        mismatches=0,
        position_mismatches=0,
        active_quarantine_count=0,
    )

    assert controller.state == "recovering"
    assert controller.last_event["event_type"] == "system_recovering"
    assert controller.last_event["reason"] == "platform_recovery_readiness_not_met"
    assert "market_data_service_missing" in controller.last_event["metadata"]["recovery_readiness"]["blockers"]


@pytest.mark.asyncio
async def test_runtime_reconciler_degrades_running_system_on_stale_platform_readiness():
    readiness = get_recovery_readiness_service()
    readiness.clear()
    now = datetime.now().astimezone()
    readiness.mark(
        "bar_pipeline",
        ready=True,
        reason="old_bar_publish",
        metadata={"ttl_sec": 1},
        now=now - timedelta(seconds=5),
    )
    controller = SystemHealthController()
    await controller.publish(
        event_type="system_resumed",
        reason="initial_clean_start",
        source="test",
        metadata={
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": True,
            "position_ledger_clean": True,
            "strategies_paused_ack": True,
            "platform_recovery_ready": True,
            "recovery_readiness": {"ready": True, "blockers": []},
        },
    )
    rec = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        system_health_controller=controller,
        recovery_required_components=["bar_pipeline"],
    )

    await rec._publish_runtime_health_if_needed(
        ghosts=0,
        unresolved_orphans=0,
        mismatches=0,
        position_mismatches=0,
        active_quarantine_count=0,
    )

    assert controller.state == "degraded"
    assert controller.last_event["event_type"] == "system_degraded"
    assert controller.last_event["reason"] == "platform_recovery_readiness_stale"
    assert "bar_pipeline_stale" in controller.last_event["metadata"]["recovery_readiness"]["blockers"]
    readiness.clear()
