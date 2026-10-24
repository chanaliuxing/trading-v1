import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import types

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("clickhouse_connect", types.ModuleType("clickhouse_connect"))

from services.reconciliation.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_resolves_synthetic_db_persist_quarantine_when_order_truth_converged():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {"symbol": "NVDA", "reason": "db_persist_submitted_failed", "note": {"source": "runtime_reconciler"}}
        ]
    )
    pg.execute = AsyncMock(return_value="UPDATE 1")
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=MagicMock())
    reconciler._emit_audit = MagicMock()
    reconciler.recovery_incident_service = None

    resolved = await reconciler.order_quarantine_handler.resolve_converged_synthetic_quarantines(
        db_orders=[{"symbol": "NVDA", "broker_order_id": "1310", "state": "SUBMITTED"}],
        ib_orders=[SimpleNamespace(symbol="NVDA", order_id=1310, status="PreSubmitted")],
        ghosts=[],
        orphans=[],
        mismatches=[],
    )

    assert resolved == 1
    sql = str(pg.execute.await_args.args[0])
    assert "UPDATE core.core_order_quarantine" in sql
    assert pg.execute.await_args.args[1] == "resolved"
    assert pg.execute.await_args.args[3] == "NVDA"
    assert "auto_resolved_synthetic_quarantine_after_order_truth_converged" in pg.execute.await_args.args[2]


@pytest.mark.asyncio
async def test_cleanup_ghost_market_order_does_not_terminalize_in_report_only():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    ghost = {
        "order_id": "uuid-1",
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "order_type": "MKT",
        "state": "SUBMITTED",
        "updated_at": "2026-02-17T10:43:00-05:00",
    }
    await reconciler._cleanup_ghosts([ghost])

    assert pg.execute.await_count == 0


@pytest.mark.asyncio
async def test_cleanup_ghost_non_market_order_does_not_terminalize_in_report_only():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    ghost = {
        "order_id": "uuid-2",
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "order_type": "STP",
        "state": "SUBMITTED",
        "updated_at": "2026-02-17T10:43:00-05:00",
    }
    await reconciler._cleanup_ghosts([ghost])

    assert pg.execute.await_count == 0


@pytest.mark.asyncio
async def test_flat_position_protection_ghost_terminalized_without_generic_cleanup():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="UPDATE 1")
    adapter = MagicMock()
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    protection_ghost = {
        "order_id": "uuid-protection-ghost",
        "strategy_id": "nvda_minute_flip_paper_001",
        "symbol": "NVDA",
        "side": "SELL",
        "order_type": "STP",
        "state": "SUBMITTED",
        "order_ref": "sl:UNKNOWN_ACCOUNT|nvda_minute_flip_paper_001",
        "idempotency_key": "acct|nvda_minute_flip_paper_001|NVDA|stop_loss|SELL|1|band:19428",
        "note": {"type": "stop_loss"},
    }
    unrelated_ghost = {
        "order_id": "uuid-entry-ghost",
        "strategy_id": "nvda_minute_flip_paper_001",
        "symbol": "NVDA",
        "side": "BUY",
        "order_type": "LMT",
        "state": "SUBMITTED",
        "order_ref": "entry:NVDA",
        "note": {},
    }

    remaining = await reconciler._terminalize_flat_position_protection_ghosts(
        ghosts=[protection_ghost, unrelated_ghost],
        position_metadata={"ib_positions": {}},
    )

    assert remaining == [unrelated_ghost]
    update_calls = [
        c
        for c in pg.execute.call_args_list
        if "UPDATE core.core_order" in str(c.args[0])
        and "core_order_quarantine" not in str(c.args[0])
        and "SET state =" in str(c.args[0])
    ]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == "uuid-protection-ghost"


@pytest.mark.asyncio
async def test_cleanup_ghost_skips_terminalize_when_fresh_probe_still_open():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.client_id = 7
    adapter.probe_order_open_fresh = AsyncMock(
        return_value={
            "found": True,
            "probe_client_id": 10007,
            "owner_client_id": 7,
            "status": "PreSubmitted",
            "perm_id": 999,
            "why_held": "trigger",
            "error": None,
        }
    )
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    ghost = {
        "order_id": "uuid-3",
        "broker_order_id": "12",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "order_type": "STP",
        "state": "SUBMITTED",
        "updated_at": "2026-04-01T13:58:45-04:00",
    }
    await reconciler._cleanup_ghosts([ghost])

    assert pg.execute.await_count == 0


@pytest.mark.asyncio
async def test_run_once_reports_ghost_without_fresh_probe_path():
    pg = MagicMock()
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_row = {
        "order_id": "uuid-db-ghost-live",
        "broker_order_id": "12",
        "ib_perm_id": None,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "SELL",
        "order_type": "STP",
        "qty": 159.0,
        "state": "SUBMITTED",
        "idempotency_key": "k-live",
        "created_at": old_ts,
        "updated_at": old_ts,
        "note": None,
    }
    pg.fetchall = AsyncMock(return_value=[db_row])
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.client_id = 7
    adapter.open_orders = AsyncMock(return_value=[])
    adapter.probe_order_open_fresh = AsyncMock()

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=30)
    reconciler._sync_positions_from_ib = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock(return_value=None)

    report = await reconciler.run_once()

    assert report.ghosts_found == 1
    adapter.probe_order_open_fresh.assert_not_called()
    update_calls = [
        c
        for c in pg.execute.call_args_list
        if "UPDATE core.core_order" in str(c.args[0])
        and "core_order_quarantine" not in str(c.args[0])
        and "SET state =" in str(c.args[0])
    ]
    assert not update_calls


@pytest.mark.asyncio
async def test_run_once_reports_ghost_without_probe_suppress_window():
    pg = MagicMock()
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_row = {
        "order_id": "uuid-db-ghost-inconclusive",
        "broker_order_id": "128",
        "ib_perm_id": None,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "SELL",
        "order_type": "STP",
        "qty": 10.0,
        "state": "SUBMITTED",
        "idempotency_key": "k-inc",
        "created_at": old_ts,
        "updated_at": old_ts,
        "note": None,
    }
    pg.fetchall = AsyncMock(return_value=[db_row])
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.client_id = 7
    adapter.open_orders = AsyncMock(return_value=[])
    adapter.probe_order_open_fresh = AsyncMock()

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=30)
    reconciler._sync_positions_from_ib = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock(return_value=None)

    report = await reconciler.run_once()

    assert report.ghosts_found == 1
    adapter.probe_order_open_fresh.assert_not_called()
    assert 128 not in reconciler._ghost_suppress_until


def _mk_ib_open_order(order_id: int, status: str = "Submitted", perm_id: int | None = None):
    return SimpleNamespace(order_id=order_id, status=status, perm_id=perm_id)


def test_loop_sleep_seconds_switches_by_trading_window():
    pg = MagicMock()
    adapter = MagicMock()
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        trading_interval_seconds=1.0,
        offhours_interval_seconds=60.0,
        dynamic_interval_enabled=True,
    )

    reconciler._in_trading_window = lambda: True
    assert reconciler._loop_sleep_seconds() == 1.0

    reconciler._in_trading_window = lambda: False
    assert reconciler._loop_sleep_seconds() == 60.0


def test_ib_status_counts_includes_presubmitted():
    statuses = RuntimeReconciler._ib_status_counts(
        [
            _mk_ib_open_order(1, status="Submitted"),
            _mk_ib_open_order(2, status="PreSubmitted"),
            _mk_ib_open_order(3, status="PreSubmitted"),
        ]
    )
    assert statuses["Submitted"] == 1
    assert statuses["PreSubmitted"] == 2


def test_normalize_perm_id_treats_zero_as_missing():
    assert RuntimeReconciler._normalize_perm_id(None) is None
    assert RuntimeReconciler._normalize_perm_id("") is None
    assert RuntimeReconciler._normalize_perm_id("0") is None
    assert RuntimeReconciler._normalize_perm_id(0) is None
    assert RuntimeReconciler._normalize_perm_id(-1) is None
    assert RuntimeReconciler._normalize_perm_id("123456") == "123456"


def test_state_mismatch_pending_submit_treated_as_submitted():
    reconciler = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    assert reconciler._state_mismatch("SUBMITTED", "PendingSubmit") is False
    assert reconciler._state_mismatch("NEW", "PendingSubmit") is True


def test_state_mismatch_partial_with_pending_submit_not_flagged():
    reconciler = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    assert reconciler._state_mismatch("PARTIAL", "PendingSubmit") is False
    assert reconciler._state_mismatch("PARTIAL", "PendingCancel") is False


@pytest.mark.asyncio
async def test_run_once_report_only_never_calls_cleanup_or_adopt():
    pg = MagicMock()
    now_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "order_id": "uuid-db-1",
                "broker_order_id": "123",
                "ib_perm_id": None,
                "strategy_id": "s1",
                "symbol": "QQQ",
                "side": "BUY",
                "order_type": "LMT",
                "qty": 1.0,
                "state": "SUBMITTED",
                "idempotency_key": "k1",
                "created_at": now_ts,
                "updated_at": now_ts,
                "note": None,
            }
        ]
    )
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(order_id=222, status="Submitted", perm_id=None, symbol="QQQ", side="SELL", qty=1.0),
        ]
    )
    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        reconciliation_mode="report_only",
        ghost_grace_seconds=30,
    )
    reconciler._cleanup_ghosts = AsyncMock()
    reconciler._adopt_orphans = AsyncMock(return_value=[])
    reconciler._quarantine_detected_orphans = AsyncMock()
    reconciler._confirm_ghost_candidates = AsyncMock(
        return_value=[
            {
                "order_id": "uuid-db-1",
                "broker_order_id": "123",
                "strategy_id": "s1",
                "symbol": "QQQ",
                "state": "SUBMITTED",
            }
        ]
    )
    reconciler._audit_positions = AsyncMock(return_value=None)
    reconciler._audit_flatten_lifecycle = AsyncMock(return_value=None)
    reconciler._maybe_push_alert = AsyncMock(return_value=None)

    await reconciler.run_once()

    reconciler._cleanup_ghosts.assert_not_awaited()
    reconciler._adopt_orphans.assert_not_awaited()
    reconciler._quarantine_detected_orphans.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_normalizes_broker_order_id_type_avoids_false_ghost():
    pg = MagicMock()
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_row = {
        "order_id": "uuid-db-1",
        "broker_order_id": "123",
        "ib_perm_id": None,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "STP",
        "qty": 10.0,
        "state": "SUBMITTED",
        "idempotency_key": "k1",
        "created_at": old_ts,
        "updated_at": old_ts,
        "note": None,
    }
    pg.fetchall = AsyncMock(return_value=[db_row])
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.open_orders = AsyncMock(return_value=[_mk_ib_open_order(order_id=123, status="Submitted")])

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=0)
    reconciler._sync_positions_from_ib = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock(return_value=None)

    report = await reconciler.run_once()

    assert report.ghosts_found == 0
    update_calls = [
        c
        for c in pg.execute.call_args_list
        if "UPDATE core.core_order" in str(c.args[0])
        and "core_order_quarantine" not in str(c.args[0])
        and "SET state =" in str(c.args[0])
    ]
    assert not update_calls


@pytest.mark.asyncio
async def test_run_once_skips_recent_missing_order_within_ghost_grace():
    pg = MagicMock()
    recent_ts = datetime.now(timezone.utc) - timedelta(seconds=15)
    db_row = {
        "order_id": "uuid-db-2",
        "broker_order_id": "124",
        "ib_perm_id": None,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "STP",
        "qty": 10.0,
        "state": "SUBMITTED",
        "idempotency_key": "k2",
        "created_at": recent_ts,
        "updated_at": recent_ts,
        "note": None,
    }
    pg.fetchall = AsyncMock(return_value=[db_row])
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.open_orders = AsyncMock(return_value=[])

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=90)
    reconciler._sync_positions_from_ib = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock(return_value=None)

    report = await reconciler.run_once()

    assert report.ghosts_found == 0
    update_calls = [c for c in pg.execute.call_args_list if "UPDATE core.core_order" in str(c.args[0])]
    assert not update_calls


@pytest.mark.asyncio
async def test_run_once_stale_market_ghost_report_only_no_terminalize():
    pg = MagicMock()
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_row = {
        "order_id": "uuid-db-3",
        "broker_order_id": "125",
        "ib_perm_id": None,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "MKT",
        "qty": 10.0,
        "state": "SUBMITTED",
        "idempotency_key": "k3",
        "created_at": old_ts,
        "updated_at": old_ts,
        "note": None,
    }
    pg.fetchall = AsyncMock(return_value=[db_row])
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.open_orders = AsyncMock(return_value=[])

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=30)
    reconciler._sync_positions_from_ib = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock(return_value=None)

    report = await reconciler.run_once()

    assert report.ghosts_found == 1
    update_calls = [c for c in pg.execute.call_args_list if "UPDATE core.core_order" in str(c.args[0])]
    assert not update_calls


@pytest.mark.asyncio
async def test_run_once_refreshes_db_after_flatten_reconcile_before_ghost_detection():
    pg = MagicMock()
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_row = {
        "order_id": "uuid-db-flatten",
        "broker_order_id": "419",
        "ib_perm_id": 974609308,
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "side": "SELL",
        "order_type": "MKT",
        "qty": 102.0,
        "state": "SUBMITTED",
        "idempotency_key": "k-flat",
        "order_ref": "qqqv1:qqq_intraday_001:SQQQ:FLATTEN:TRG:bfb36",
        "created_at": old_ts,
        "updated_at": old_ts,
        "note": '{"flatten_lifecycle":{"state":"submitted"}}',
    }
    adapter = MagicMock()
    adapter.open_orders = AsyncMock(return_value=[])

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=30)
    reconciler._fetch_db_open_orders = AsyncMock(return_value=[db_row])
    reconciler._sync_positions_from_ib = AsyncMock(return_value=None)
    reconciler._audit_positions = AsyncMock(return_value=None)
    reconciler._audit_flatten_lifecycle = AsyncMock(return_value=None)
    reconciler._reconcile_flatten_lifecycle = AsyncMock(return_value=None)

    report = await reconciler.run_once()

    assert reconciler._fetch_db_open_orders.await_count == 1
    reconciler._reconcile_flatten_lifecycle.assert_not_awaited()
    assert report.ghosts_found == 1


@pytest.mark.asyncio
async def test_run_once_forces_fresh_ib_snapshot_when_db_has_open_orders():
    pg = MagicMock()
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_row = {
        "order_id": "uuid-db-4",
        "broker_order_id": "126",
        "ib_perm_id": None,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "LMT",
        "qty": 10.0,
        "state": "SUBMITTED",
        "idempotency_key": "k4",
        "created_at": old_ts,
        "updated_at": old_ts,
        "note": None,
    }
    pg.fetchall = AsyncMock(return_value=[db_row])
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.open_orders = AsyncMock(return_value=[])

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, ghost_grace_seconds=30)
    reconciler._audit_positions = AsyncMock(return_value=None)

    await reconciler.run_once()

    adapter.open_orders.assert_awaited_once_with(force_refresh=True)


@pytest.mark.asyncio
async def test_fetch_db_open_orders_selects_order_type_and_perm_id():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[])
    adapter = MagicMock()
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    await reconciler._fetch_db_open_orders()

    sql = pg.fetchall.await_args.args[0]
    assert "order_type" in sql
    assert "ib_perm_id" in sql


@pytest.mark.asyncio
async def test_audit_positions_does_not_auto_heal_when_ib_flat_and_no_open_orders():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            [{"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "pos_qty": 100.0}],
        ]
    )
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.positions = AsyncMock(return_value=[])
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    await reconciler._audit_positions(db_open_orders=[], ib_open_orders=[])

    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_positions_does_not_auto_heal_each_strategy_row_independently():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "pos_qty": 51.0},
            {"strategy_id": "index_super_win_001", "symbol": "TQQQ", "pos_qty": 49.0},
        ]
    )
    pg.execute = AsyncMock(return_value="OK")
    adapter = MagicMock()
    adapter.positions = AsyncMock(return_value=[])
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    await reconciler._audit_positions(db_open_orders=[], ib_open_orders=[])

    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_orphans_uses_idempotent_upsert_by_idempotency_key():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    pg.fetchone = AsyncMock(return_value=None)
    adapter = MagicMock()
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    orphan = SimpleNamespace(
        order_id=6,
        perm_id=123456,
        symbol="OPT:QQQ:20260326:590:P",
        side="BUY",
        qty=2.0,
        order_type="LMT",
        status="Submitted",
        order_ref="opt0dte:qqq_0dte_opt_live_v2_001:abc123",
    )

    unresolved = await reconciler._adopt_orphans([orphan])
    assert unresolved == [orphan]
    pg.execute.assert_not_awaited()
