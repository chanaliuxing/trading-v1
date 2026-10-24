import sys
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from services.exec.trace import TraceContext

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.execution_service import ExecutionService
from services.decision.translator import ExecPlan


class _FakeConn:
    def __init__(self, *, pending_row=None, pending_row_cross=None, pending_row_same=None):
        self.pending_row = pending_row
        self.pending_row_cross = pending_row_cross
        self.pending_row_same = pending_row_same
        self.exec_calls = []
        self.fetch_calls = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args):
        self.exec_calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        if "FROM core.core_order" in sql and "state IN ('NEW', 'SUBMITTED', 'PARTIAL')" in sql:
            if "strategy_id <> $2" in sql:
                return self.pending_row_cross if self.pending_row_cross is not None else self.pending_row
            if "strategy_id = $2" in sql:
                return self.pending_row_same if self.pending_row_same is not None else self.pending_row
            return self.pending_row
        return None


class _FakePg:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire_conn(self):
        yield self._conn


@pytest.mark.asyncio
async def test_persist_order_new_with_entry_lock_blocks_when_pending_exists():
    conn = _FakeConn(pending_row={"strategy_id": "other", "n": 2})
    pg = _FakePg(conn)
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=True)

    ok, reason, meta = await svc._persist_order_new_with_entry_lock(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        plan=ExecPlan(order_type="MKT", side="BUY", qty=1, limit_px=None, stop_px=None, tif="DAY", price_collar_pct=3.0, oco={}, child={"type": "NONE"}, notes={}),
        idempotency_key="idem1",
        envelope_extras={},
        trace_ctx=None,
    )

    assert ok is False
    assert reason == "entry_isolation_blocked_pending_order"
    assert meta.get("other_strategy_id") == "other"
    # Should have taken advisory lock.
    assert any("pg_advisory_xact_lock" in sql for (sql, _args) in conn.exec_calls)
    # Should NOT have inserted the order row.
    assert not any("INSERT INTO core.core_order" in sql for (sql, _args) in conn.exec_calls)


@pytest.mark.asyncio
async def test_persist_order_new_with_entry_lock_inserts_when_no_pending():
    conn = _FakeConn(pending_row=None)
    pg = _FakePg(conn)
    svc = ExecutionService(pg=pg, risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=True)

    ok, reason, meta = await svc._persist_order_new_with_entry_lock(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        plan=ExecPlan(order_type="MKT", side="BUY", qty=1, limit_px=None, stop_px=None, tif="DAY", price_collar_pct=3.0, oco={}, child={"type": "NONE"}, notes={}),
        idempotency_key="idem1",
        envelope_extras={},
        trace_ctx=None,
    )

    assert ok is True
    assert reason == "ok"
    assert meta == {}
    assert any("pg_advisory_xact_lock" in sql for (sql, _args) in conn.exec_calls)
    assert any("INSERT INTO core.core_order" in sql for (sql, _args) in conn.exec_calls)


@pytest.mark.asyncio
async def test_persist_order_new_with_entry_lock_emits_lock_acquired_audit():
    conn = _FakeConn(pending_row=None)
    pg = _FakePg(conn)
    audit_writer = MagicMock()
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        audit_writer=audit_writer,
    )

    ok, reason, meta = await svc._persist_order_new_with_entry_lock(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        plan=ExecPlan(order_type="MKT", side="BUY", qty=1, limit_px=None, stop_px=None, tif="DAY", price_collar_pct=3.0, oco={}, child={"type": "NONE"}, notes={}),
        idempotency_key="idem1",
        envelope_extras={},
        trace_ctx=TraceContext(
            strategy_id="s1",
            strategy_run_id="run1",
            trigger_id="trg1",
            decision_id="d1",
            trace_id="trace1",
        ),
    )

    assert ok is True
    assert reason == "ok"
    stages = [call.kwargs.get("stage") for call in audit_writer.emit.call_args_list]
    assert "entry_isolation_lock_acquired" in stages


@pytest.mark.asyncio
async def test_persist_order_new_with_entry_lock_option_override_allows_cross_strategy_same_symbol():
    conn = _FakeConn(
        pending_row_cross={"strategy_id": "other_strategy", "n": 2},
        pending_row_same=None,
    )
    pg = _FakePg(conn)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_mode="cross_strategy",
    )

    ok, reason, meta = await svc._persist_order_new_with_entry_lock(
        decision_id="d1",
        strategy_id="s1",
        symbol="OPT:QQQ:20260326:C:530",
        plan=ExecPlan(order_type="MKT", side="BUY", qty=1, limit_px=None, stop_px=None, tif="DAY", price_collar_pct=3.0, oco={}, child={"type": "NONE"}, notes={}),
        idempotency_key="idem1",
        envelope_extras={
            "instrument_type": "OPTION",
            "allow_same_option_symbol_cross_strategy": True,
        },
        trace_ctx=None,
        entry_isolation_mode_override="strategy_only",
    )

    assert ok is True
    assert reason == "ok"
    assert meta == {}
    assert any("pg_advisory_xact_lock" in sql for (sql, _args) in conn.exec_calls)
    assert any("INSERT INTO core.core_order" in sql for (sql, _args) in conn.exec_calls)


@pytest.mark.asyncio
async def test_persist_order_new_with_entry_lock_option_override_still_blocks_same_strategy_duplicate():
    conn = _FakeConn(
        pending_row_cross={"strategy_id": "other_strategy", "n": 2},
        pending_row_same={"strategy_id": "s1", "n": 1},
    )
    pg = _FakePg(conn)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_mode="cross_strategy",
    )

    ok, reason, meta = await svc._persist_order_new_with_entry_lock(
        decision_id="d1",
        strategy_id="s1",
        symbol="OPT:QQQ:20260326:C:530",
        plan=ExecPlan(order_type="MKT", side="BUY", qty=1, limit_px=None, stop_px=None, tif="DAY", price_collar_pct=3.0, oco={}, child={"type": "NONE"}, notes={}),
        idempotency_key="idem1",
        envelope_extras={
            "instrument_type": "OPTION",
            "allow_same_option_symbol_cross_strategy": True,
        },
        trace_ctx=None,
        entry_isolation_mode_override="strategy_only",
    )

    assert ok is False
    assert reason == "entry_isolation_blocked_same_strategy_pending_order"
    assert meta.get("same_strategy_id") == "s1"
    assert not any("INSERT INTO core.core_order" in sql for (sql, _args) in conn.exec_calls)
