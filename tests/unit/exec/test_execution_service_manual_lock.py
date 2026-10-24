import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService


def _result(*, extras: dict) -> PipelineResult:
    return PipelineResult(
        status="APPROVED",
        decision_id="d-lock",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={
            "trigger_id": "trg-1",
            "strategy_run_id": "run-1",
            "trace_id": "trace-1",
            "envelope_extras": dict(extras),
        },
    )


def test_entry_blocked_when_manual_intervention_lock_active():
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(
        return_value={
            "reason": "manual_flatten",
            "created_ts": 1.0,
            "expire_ts": 9999999999.0,
            "context": {"intervention_id": "intv-1"},
        }
    )
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )

    out = __import__("asyncio").run(svc.execute(_result(extras={})))

    assert out.success is False
    assert out.stage == "validation"
    assert out.reason == "manual_intervention_lock_active"
    assert lock_store.get_active.await_count == 1
    assert lock_store.lock.await_count == 0


def test_entry_manual_lock_lookup_is_strategy_instance_scoped():
    pg = MagicMock()
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=False, reason="blocked", hits=[]))
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value=None)
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )

    out = __import__("asyncio").run(
        svc.execute(_result(extras={"strategy_instance_id": "qqq_intraday_001_b"}))
    )

    assert lock_store.get_active.await_args.kwargs["strategy_instance_id"] == "qqq_intraday_001_b"
    assert out.reason != "manual_intervention_lock_active"


def test_entry_blocked_when_manual_intervention_lock_lookup_fails():
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(side_effect=RuntimeError("store down"))
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )

    out = __import__("asyncio").run(svc.execute(_result(extras={})))

    assert out.success is False
    assert out.stage == "validation"
    assert out.reason == "manual_intervention_lock_active"
    assert out.metadata["lock_context"]["lookup_failed"] is True
    assert lock_store.get_active.await_count == 1
    assert lock_store.lock.await_count == 0


def test_manual_flatten_sets_manual_intervention_lock_before_risk():
    pg = MagicMock()
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=False, reason="blocked", hits=[]))
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value=None)
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )

    out = __import__("asyncio").run(
        svc.execute(
            _result(
                extras={
                    "manual_intervention": True,
                    "manual_intervention_id": "intv-2",
                    "manual_source": "telegram",
                    "trigger_id": "tg:1:2",
                    "flatten_action": True,
                    "close_only": True,
                    "manual_lock_ttl_sec": 600,
                }
            )
        )
    )

    assert out.success is False
    assert out.stage == "risk"
    assert lock_store.lock.await_count == 0
    assert risk.approve.await_count == 1
    env = risk.approve.await_args.kwargs["envelope"]
    assert bool(env.get("extras", {}).get("flatten_action")) is True
    assert bool(env.get("extras", {}).get("close_only")) is True


def test_manual_flatten_sets_manual_intervention_lock_after_submit():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101"})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value=None)
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101"})
    svc._get_live_position_qty = AsyncMock(return_value=10.0)
    svc._place_order = AsyncMock(
        return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=0, order_ref="ref-1")
    )

    out = __import__("asyncio").run(
        svc.execute(
            _result(
                extras={
                    "manual_intervention": True,
                    "manual_intervention_id": "intv-2",
                    "manual_source": "telegram",
                    "trigger_id": "tg:1:2",
                    "flatten_action": True,
                    "close_only": True,
                    "manual_lock_ttl_sec": 600,
                }
            )
        )
    )

    assert out.success is True
    assert out.stage == "completed"
    assert lock_store.lock.await_count == 1
    kwargs = lock_store.lock.await_args.kwargs
    assert kwargs["strategy_id"] == "qqq_intraday_001"
    assert kwargs["symbol"] == "TQQQ"
    assert kwargs["ttl_sec"] == 600


def test_non_manual_flatten_also_sets_entry_lock_after_submit():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101"})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value=None)
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101"})
    svc._get_live_position_qty = AsyncMock(return_value=10.0)
    svc._place_order = AsyncMock(
        return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=0, order_ref="ref-1")
    )

    out = __import__("asyncio").run(
        svc.execute(
            _result(
                extras={
                    "flatten_action": True,
                    "close_only": True,
                    "manual_lock_ttl_sec": 300,
                }
            )
        )
    )

    assert out.success is True
    assert out.stage == "completed"
    lock_store.lock.assert_awaited_once()
    kwargs = lock_store.lock.await_args.kwargs
    assert kwargs["reason"] == "flatten_action"
    assert kwargs["ttl_sec"] == 300


def test_strategy_scheduled_flatten_can_suppress_entry_lock_after_submit():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101"})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)
    kernel = MagicMock()
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value=None)
    lock_store.lock = AsyncMock(return_value={})

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        manual_intervention_lock_store=lock_store,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101"})
    svc._get_live_position_qty = AsyncMock(return_value=10.0)
    svc._place_order = AsyncMock(
        return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=0, order_ref="ref-1")
    )

    out = __import__("asyncio").run(
        svc.execute(
            _result(
                extras={
                    "flatten_action": True,
                    "close_only": True,
                    "suppress_manual_intervention_lock": True,
                }
            )
        )
    )

    assert out.success is True
    assert out.stage == "completed"
    lock_store.lock.assert_not_awaited()


def test_update_order_submitted_persists_flatten_lifecycle_note():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(return_value={"note": '{"decision_id":"d1"}'})
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
    )

    __import__("asyncio").run(
        svc._update_order_submitted(
            idempotency_key="idem-1",
            order_id=101,
            broker_order_id="101",
            perm_id=999,
            order_ref="optk:spy:flatten",
            envelope_extras={"flatten_action": True, "close_only": True},
        )
    )

    assert pg.fetchone.await_count == 1
    sql = pg.execute.await_args.args[0]
    assert "note = $7" in sql
    note_data = pg.execute.await_args.args[7]
    assert '"flatten_lifecycle"' in note_data
    assert '"state": "submitted"' in note_data or '"state":"submitted"' in note_data


def test_update_order_rejected_persists_flatten_lifecycle_note():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    pg.fetchone = AsyncMock(return_value={"note": '{"decision_id":"d1","flatten_lifecycle":{"state":"created"}}'})
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
    )

    __import__("asyncio").run(
        svc._update_order_rejected(
            idempotency_key="idem-1",
            reason="Broker placement failed",
            envelope_extras={"flatten_action": True, "close_only": True},
        )
    )

    sql = pg.execute.await_args.args[0]
    assert "UPDATE core.core_order" in sql
    payload = pg.execute.await_args.args[1]
    assert '"flatten_lifecycle"' in payload
    assert '"state": "rejected"' in payload or '"state":"rejected"' in payload


@pytest.mark.asyncio
async def test_entry_blocked_when_symbol_quarantine_active():
    pg = MagicMock()

    async def _fetchone(sql, *args):
        if "core.core_order_quarantine" in sql:
            return {"has_active": 1}
        return None

    pg.fetchone = AsyncMock(side_effect=_fetchone)
    pg.execute = AsyncMock(return_value=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
        trading_gate_provider=lambda _symbol: {
            "exec_ready": True,
            "market_ready": True,
            "master_truth_complete": True,
        },
    )
    svc._emit_event = AsyncMock(return_value=None)

    result = _result(extras={})

    out = await svc.execute(result)
    assert out.success is False
    assert out.reason == "symbol_quarantined"


@pytest.mark.asyncio
async def test_quarantine_lookup_does_not_run_schema_ddl():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"has_active": 1})
    pg.execute = AsyncMock(return_value=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
    )

    active = await svc._has_unresolved_symbol_quarantine(symbol="QQQ")

    assert active is True
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_quarantine_lookup_failure_fails_closed_for_entry_gate():
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=RuntimeError("db down"))
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        order_event_enabled=False,
    )

    active = await svc._has_unresolved_symbol_quarantine(symbol="QQQ")
    allowed, reason, meta = await svc._evaluate_trading_gate(
        symbol="QQQ",
        symbol_quarantine_active=active,
    )

    assert active is True
    assert allowed is False
    assert reason == "symbol_quarantined"
    assert meta["no_unresolved_quarantine_for_symbol"] is False


def test_manual_flatten_limit_guard_blocks_out_of_band_limit():
    ok, reason = ExecutionService._enforce_manual_flatten_limit_guard(
        plan=ExecPlan(
            order_type="LMT",
            side="SELL",
            qty=10,
            limit_px=0.99,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        envelope_extras={
            "manual_intervention": True,
            "flatten_action": True,
            "close_only": True,
            "manual_quote_snapshot": {"bid": 49.0, "ask": 49.1, "last": 49.05, "mid": 49.05},
        },
    )
    assert ok is False
    assert reason.startswith("manual_flatten_limit_out_of_band")


def test_manual_flatten_limit_guard_accepts_reasonable_limit():
    ok, reason = ExecutionService._enforce_manual_flatten_limit_guard(
        plan=ExecPlan(
            order_type="LMT",
            side="SELL",
            qty=10,
            limit_px=48.8,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        envelope_extras={
            "manual_intervention": True,
            "flatten_action": True,
            "close_only": True,
            "manual_quote_snapshot": {"bid": 49.0, "ask": 49.1, "last": 49.05, "mid": 49.05},
        },
    )
    assert ok is True
    assert reason == "ok"


def test_manual_flatten_limit_guard_accepts_one_tick_low_price_option_limit():
    ok, reason = ExecutionService._enforce_manual_flatten_limit_guard(
        plan=ExecPlan(
            order_type="LMT",
            side="SELL",
            qty=1,
            limit_px=0.02,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        envelope_extras={
            "manual_intervention": True,
            "flatten_action": True,
            "close_only": True,
            "manual_quote_snapshot": {"bid": 0.02, "ask": 0.021, "last": 0.02, "mid": 0.0205},
            "manual_limit_guard_max_dev_bps": 500.0,
            "manual_limit_guard_min_tick": 0.01,
        },
    )

    assert ok is True
    assert reason == "ok"


def test_manual_flatten_limit_guard_blocks_when_quote_missing_but_anchor_present():
    ok, reason = ExecutionService._enforce_manual_flatten_limit_guard(
        plan=ExecPlan(
            order_type="LMT",
            side="SELL",
            qty=10,
            limit_px=0.99,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        envelope_extras={
            "manual_intervention": True,
            "flatten_action": True,
            "close_only": True,
            "manual_quote_snapshot": {},
            "manual_anchor_px": 49.0,
        },
    )
    assert ok is False
    assert reason.startswith("manual_flatten_limit_out_of_band")


def test_manual_flatten_limit_guard_rejects_anchor_quote_conflict():
    ok, reason = ExecutionService._enforce_manual_flatten_limit_guard(
        plan=ExecPlan(
            order_type="LMT",
            side="SELL",
            qty=1,
            limit_px=198.0,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        envelope_extras={
            "manual_intervention": True,
            "flatten_action": True,
            "close_only": True,
            "manual_quote_snapshot": {"bid": 198.5, "ask": 198.7, "last": 198.6, "mid": 198.6},
            "manual_anchor_px": 1414.0,
        },
    )

    assert ok is False
    assert reason.startswith("manual_flatten_limit_anchor_quote_conflict")
