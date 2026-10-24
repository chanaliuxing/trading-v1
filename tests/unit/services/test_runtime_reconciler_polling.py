import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("clickhouse_connect", types.ModuleType("clickhouse_connect"))

from services.reconciliation.runtime_reconciler import RuntimeReconciler


def _mk_order(order_id: int, status: str, **kwargs):
    payload = {"order_id": order_id, "status": status}
    payload.update(kwargs)
    return types.SimpleNamespace(**payload)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_loop_sleep_seconds_switches_by_trading_window():
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        trading_interval_seconds=1.0,
        offhours_interval_seconds=60.0,
        dynamic_interval_enabled=True,
    )

    reconciler._in_trading_window = lambda: True
    assert reconciler._loop_sleep_seconds() == 1.0

    reconciler._in_trading_window = lambda: False
    assert reconciler._loop_sleep_seconds() == 60.0


def test_ib_status_counts_includes_presubmitted():
    counts = RuntimeReconciler._ib_status_counts(
        [
            _mk_order(1, "Submitted"),
            _mk_order(2, "PreSubmitted"),
            _mk_order(3, "PreSubmitted"),
        ]
    )
    assert counts["Submitted"] == 1
    assert counts["PreSubmitted"] == 2


@pytest.mark.anyio
async def test_maybe_push_alert_only_on_anomaly_and_respects_cooldown():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        notification_manager=manager,
        alert_cooldown_seconds=300,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._next_alert_push_ts = 0

    await reconciler._maybe_push_alert(
        db_open_count=1,
        ib_open_count=2,
        ib_pre_submitted=1,
        ghosts=0,
        orphans=0,
        mismatches=0,
        active_quarantine_count=0,
    )
    manager.publish.assert_not_called()

    await reconciler._maybe_push_alert(
        db_open_count=1,
        ib_open_count=2,
        ib_pre_submitted=1,
        ghosts=0,
        orphans=1,
        mismatches=0,
        active_quarantine_count=0,
    )
    await reconciler._maybe_push_alert(
        db_open_count=2,
        ib_open_count=3,
        ib_pre_submitted=1,
        ghosts=1,
        orphans=1,
        mismatches=0,
        active_quarantine_count=0,
    )
    manager.publish.assert_called_once()


@pytest.mark.anyio
async def test_maybe_push_alert_suppresses_self_healed_unknown_broker_quarantine_only():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        notification_manager=manager,
        alert_cooldown_seconds=300,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._next_alert_push_ts = 0

    await reconciler._maybe_push_alert(
        db_open_count=0,
        ib_open_count=1,
        ib_pre_submitted=0,
        ghosts=0,
        orphans=0,
        orphans_detected=1,
        orphans_self_healed=1,
        mismatches=0,
        active_quarantine_count=1,
        evidence={
            "active_quarantine": {
                "active_count": 1,
                "by_reason": [{"reason": "unknown_broker_order_detected", "active_count": 1}],
                "lookup_failed": False,
            }
        },
    )

    manager.publish.assert_not_called()


@pytest.mark.anyio
async def test_maybe_push_alert_fires_on_actionable_active_quarantine_without_other_anomalies():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        notification_manager=manager,
        alert_cooldown_seconds=300,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._next_alert_push_ts = 0

    await reconciler._maybe_push_alert(
        db_open_count=0,
        ib_open_count=0,
        ib_pre_submitted=0,
        ghosts=0,
        orphans=0,
        mismatches=0,
        active_quarantine_count=2,
        evidence={
            "active_quarantine": {
                "active_count": 2,
                "by_reason": [{"reason": "terminal_orphan_cancel_not_confirmed", "active_count": 2}],
                "lookup_failed": False,
            }
        },
    )

    manager.publish.assert_called_once()
    event = manager.publish.call_args.args[0]
    assert event.metadata["active_quarantine_count"] == 2


@pytest.mark.anyio
async def test_run_once_quarantines_orphans_in_report_only_mode():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    adapter = MagicMock()
    orphan = _mk_order(22, "Submitted")
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[orphan])
    reconciler._quarantine_detected_orphans = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()

    report = await reconciler.run_once()

    assert report.orphans_found == 1
    reconciler._quarantine_detected_orphans.assert_awaited_once_with([orphan])
    manager.publish.assert_called_once()


@pytest.mark.anyio
async def test_run_once_alerts_when_orphans_remain_unresolved():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    adapter = MagicMock()
    orphan = _mk_order(22, "Submitted")
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[orphan])
    reconciler._quarantine_detected_orphans = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()

    report = await reconciler.run_once()

    assert report.orphans_found == 1
    reconciler._quarantine_detected_orphans.assert_awaited_once_with([orphan])
    manager.publish.assert_called_once()


@pytest.mark.anyio
async def test_run_once_alerts_when_db_terminal_orphan_cancel_not_confirmed():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    orphan = _mk_order(
        22,
        "Submitted",
        symbol="SQQQ",
        side="SELL",
        qty=95.0,
        order_type="STP",
        order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP:TRG:test",
        perm_id=12345,
    )
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    adapter.open_orders = AsyncMock(return_value=[orphan])
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": True, "status": "Submitted"})
    adapter.client_id = 7
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-order-1",
            "strategy_id": "qqq_intraday_001",
            "state": "CANCELLED",
            "idempotency_key": "ik-1",
        }
    )
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[orphan])
    reconciler._audit_positions = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()

    report = await reconciler.run_once()

    assert report.orphans_found == 1
    adapter.cancel.assert_not_awaited()
    adapter.open_orders.assert_not_awaited()
    manager.publish.assert_called_once()
    event = manager.publish.call_args.args[0]
    assert event.metadata["orphans_found"] == 1
    assert event.metadata["orphans_detected"] == 1
    assert event.metadata["orphans_self_healed"] == 0
    assert event.metadata["evidence"]["orphan_unresolved_samples"][0]["order_ref"] == orphan.order_ref
    assert event.metadata["evidence"]["ib_status_counts"]["Submitted"] == 1


@pytest.mark.anyio
async def test_run_once_uses_owner_client_targeted_cancel_for_terminal_orphan():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    orphan = _mk_order(
        22,
        "Submitted",
        symbol="SQQQ",
        side="SELL",
        qty=95.0,
        order_type="STP",
        order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP:TRG:test",
        perm_id=12345,
    )
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    adapter.cancel_with_owner = AsyncMock(return_value=True)
    adapter.open_orders = AsyncMock(side_effect=[[orphan], []])
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": True, "status": "PreSubmitted"})
    adapter.client_id = 207
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-order-1",
            "strategy_id": "qqq_intraday_001",
            "state": "CANCELLED",
            "idempotency_key": "ik-1",
        }
    )
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[orphan])
    reconciler._audit_positions = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()

    report = await reconciler.run_once()

    assert report.orphans_found == 1
    adapter.cancel.assert_not_awaited()
    adapter.cancel_with_owner.assert_not_awaited()
    manager.publish.assert_called_once()


@pytest.mark.anyio
async def test_run_once_suppresses_alert_when_db_terminal_orphan_cancel_confirmed():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    orphan = _mk_order(
        22,
        "Submitted",
        symbol="SQQQ",
        side="SELL",
        qty=95.0,
        order_type="STP",
        order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP:TRG:test",
        perm_id=12345,
    )
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    adapter.open_orders = AsyncMock(return_value=[])
    adapter.probe_order_open_fresh = AsyncMock()
    adapter.client_id = 7
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-order-1",
            "strategy_id": "qqq_intraday_001",
            "state": "CANCELLED",
            "idempotency_key": "ik-1",
        }
    )
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[orphan])
    reconciler._audit_positions = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()

    report = await reconciler.run_once()

    assert report.orphans_found == 1
    adapter.cancel.assert_not_awaited()
    adapter.open_orders.assert_not_awaited()
    adapter.probe_order_open_fresh.assert_not_called()
    manager.publish.assert_called_once()


@pytest.mark.anyio
async def test_run_once_suppresses_alert_when_db_terminal_orphan_absent_on_fresh_probe():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    orphan = _mk_order(
        22,
        "Submitted",
        symbol="SQQQ",
        side="SELL",
        qty=95.0,
        order_type="STP",
        order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP:TRG:test",
        perm_id=12345,
    )
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    # Simulate live-adapter shadow still showing order open after cancel.
    adapter.open_orders = AsyncMock(return_value=[_mk_order(22, "PendingCancel", symbol="SQQQ", order_ref=orphan.order_ref)])
    # Fresh probe says broker no longer has this order.
    adapter.probe_order_open_fresh = AsyncMock(return_value={"found": False, "probe_client_id": 19000})
    adapter.client_id = 7
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-order-1",
            "strategy_id": "qqq_intraday_001",
            "state": "CANCELLED",
            "idempotency_key": "ik-1",
        }
    )
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[orphan])
    reconciler._audit_positions = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()

    report = await reconciler.run_once()

    assert report.orphans_found == 1
    adapter.cancel.assert_not_awaited()
    adapter.open_orders.assert_not_awaited()
    adapter.probe_order_open_fresh.assert_not_called()
    manager.publish.assert_called_once()


@pytest.mark.anyio
async def test_run_once_report_only_does_not_cancel_flat_protective_orders():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    adapter.open_orders = AsyncMock(return_value=[])
    db_order = {
        "order_id": "db-376",
        "broker_order_id": "376",
        "strategy_id": "qqq_1dte_ma10_reject_0324_v2_001",
        "symbol": "OPT:QQQ:20260410:609:C",
        "state": "SUBMITTED",
        "order_ref": "sl:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
        "idempotency_key": "",
        "note": "{}",
    }
    ib_order = _mk_order(
        376,
        "PreSubmitted",
        symbol="OPT:QQQ:20260410:609:C",
        side="SELL",
        qty=2.0,
        order_type="STP",
        order_ref="sl:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
        perm_id=974608460,
    )
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[db_order])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[ib_order])
    reconciler._audit_positions = AsyncMock()
    reconciler._reconcile_flatten_lifecycle = AsyncMock()
    reconciler._audit_flatten_lifecycle = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()
    reconciler._adopt_orphans = AsyncMock(return_value=[])

    await reconciler.run_once()

    adapter.cancel.assert_not_awaited()
    assert not any("UPDATE core.core_order" in str(c.args[0]) for c in pg.execute.await_args_list if c.args)


@pytest.mark.anyio
async def test_run_once_auto_cancels_flat_protective_orders_when_explicitly_enabled():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    adapter.open_orders = AsyncMock(return_value=[])
    adapter.positions = AsyncMock(return_value=[])
    db_order = {
        "order_id": "db-936",
        "broker_order_id": "936",
        "strategy_id": "nvda_minute_flip_paper_001",
        "symbol": "NVDA",
        "state": "SUBMITTED",
        "side": "SELL",
        "qty": 1.0,
        "order_type": "STP",
        "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
        "idempotency_key": "",
        "note": "{}",
    }
    ib_order = _mk_order(
        936,
        "PreSubmitted",
        symbol="NVDA",
        side="SELL",
        qty=1.0,
        order_type="STP",
        order_ref="sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
        perm_id=178521796,
    )
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
        flat_protective_auto_cancel_enabled=True,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[db_order])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[ib_order])
    reconciler._audit_positions = AsyncMock()
    reconciler._reconcile_flatten_lifecycle = AsyncMock()
    reconciler._audit_flatten_lifecycle = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()
    reconciler._adopt_orphans = AsyncMock(return_value=[])

    await reconciler.run_once()

    adapter.cancel.assert_awaited_once_with(936)
    assert any("UPDATE core.core_order" in str(c.args[0]) for c in pg.execute.await_args_list if c.args)


@pytest.mark.anyio
async def test_run_once_skips_flat_protective_cancel_when_broker_position_nonzero():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    db_order = {
        "order_id": "db-376",
        "broker_order_id": "376",
        "strategy_id": "qqq_1dte_ma10_reject_0324_v2_001",
        "symbol": "OPT:QQQ:20260410:609:C",
        "state": "SUBMITTED",
        "order_ref": "tp:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
        "idempotency_key": "",
        "note": "{}",
    }
    ib_order = _mk_order(
        376,
        "PreSubmitted",
        symbol="OPT:QQQ:20260410:609:C",
        side="SELL",
        qty=2.0,
        order_type="LMT",
        order_ref="tp:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
        perm_id=974608461,
    )
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[db_order])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[ib_order])
    reconciler._load_db_position_qty = AsyncMock(return_value=0.0)
    reconciler._load_broker_symbol_position_qty = AsyncMock(return_value=2.0)
    reconciler._audit_positions = AsyncMock()
    reconciler._reconcile_flatten_lifecycle = AsyncMock()
    reconciler._audit_flatten_lifecycle = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()
    reconciler._adopt_orphans = AsyncMock(return_value=[])

    await reconciler.run_once()

    adapter.cancel.assert_not_awaited()


@pytest.mark.anyio
async def test_run_once_skips_flat_protective_cancel_when_broker_position_unknown():
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)
    adapter = MagicMock()
    adapter.cancel = AsyncMock(return_value=True)
    db_order = {
        "order_id": "db-376",
        "broker_order_id": "376",
        "strategy_id": "qqq_1dte_ma10_reject_0324_v2_001",
        "symbol": "OPT:QQQ:20260410:609:C",
        "state": "SUBMITTED",
        "order_ref": "sl:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
        "idempotency_key": "",
        "note": "{}",
    }
    ib_order = _mk_order(
        376,
        "PreSubmitted",
        symbol="OPT:QQQ:20260410:609:C",
        side="SELL",
        qty=2.0,
        order_type="STP",
        order_ref="sl:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
        perm_id=974608460,
    )
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        notification_manager=manager,
    )
    reconciler._in_trading_window = lambda: True
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[db_order])
    reconciler._fetch_ib_open_orders = AsyncMock(return_value=[ib_order])
    reconciler._audit_positions = AsyncMock()
    reconciler._reconcile_flatten_lifecycle = AsyncMock()
    reconciler._audit_flatten_lifecycle = AsyncMock()
    reconciler._sync_positions_from_ib = AsyncMock()
    reconciler._adopt_orphans = AsyncMock(return_value=[])
    reconciler._emit_audit = MagicMock()

    await reconciler.run_once()

    adapter.cancel.assert_not_awaited()
    assert reconciler._emit_audit.call_count >= 1
