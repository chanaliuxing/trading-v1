import sys
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.execution_service import ExecutionService
from services.decision.translator import ExecPlan


class _FakeConn:
    def __init__(self, *, pending_row_cross=None, pending_row_same=None):
        self.pending_row_cross = pending_row_cross
        self.pending_row_same = pending_row_same
        self.exec_calls = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args):
        self.exec_calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        if "FROM core.core_order" in sql and "state IN ('NEW', 'SUBMITTED', 'PARTIAL')" in sql:
            if "strategy_id <> $2" in sql:
                return self.pending_row_cross
            if "strategy_id = $2" in sql:
                return self.pending_row_same
        return None


class _FakePg:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire_conn(self):
        yield self._conn

    async def fetchone(self, sql: str, *args):
        # Used by _enforce_entry_isolation path.
        if "FROM core.core_order" in sql and "state IN ('NEW', 'SUBMITTED', 'PARTIAL')" in sql:
            if "strategy_id <> $2" in sql:
                return self._conn.pending_row_cross
            if "strategy_id = $2" in sql:
                return self._conn.pending_row_same
        return None


def _plan() -> ExecPlan:
    return ExecPlan(
        order_type="MKT",
        side="BUY",
        qty=1.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )


@pytest.mark.asyncio
async def test_strategy_only_allows_other_strategy_same_symbol() -> None:
    conn = _FakeConn(
        pending_row_cross={"strategy_id": "other_strategy", "n": 2},
        pending_row_same=None,
    )
    pg = _FakePg(conn)
    kernel = MagicMock()
    kernel.broker = MagicMock()
    kernel.broker.position_qty = AsyncMock(return_value=999.0)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_mode="strategy_only",
    )

    ok, reason, meta = await svc._enforce_entry_isolation(strategy_id="s1", symbol="TQQQ")
    assert ok is True
    assert reason == "ok"
    assert meta == {}
    # strategy_only should not use account-level position blocker.
    assert kernel.broker.position_qty.await_count == 0


@pytest.mark.asyncio
async def test_strategy_only_blocks_same_strategy_duplicate_pending() -> None:
    conn = _FakeConn(
        pending_row_cross=None,
        pending_row_same={"strategy_id": "s1", "n": 1},
    )
    pg = _FakePg(conn)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_mode="strategy_only",
    )

    ok, reason, meta = await svc._persist_order_new_with_entry_lock(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        plan=_plan(),
        idempotency_key="idem1",
        envelope_extras={},
        trace_ctx=None,
    )

    assert ok is False
    assert reason == "entry_isolation_blocked_same_strategy_pending_order"
    assert meta.get("same_strategy_id") == "s1"


def test_entry_isolation_empty_symbol_list_emits_scope_all_symbols_audit() -> None:
    audit_writer = MagicMock()
    ExecutionService(
        pg=MagicMock(),
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_symbols=[],
        audit_writer=audit_writer,
    )

    stages = [call.kwargs.get("stage") for call in audit_writer.emit.call_args_list]
    assert "entry_isolation_scope_all_symbols" in stages


@pytest.mark.asyncio
async def test_cross_strategy_live_position_block_emits_local_audit() -> None:
    conn = _FakeConn(pending_row_cross=None, pending_row_same=None)
    pg = _FakePg(conn)
    kernel = MagicMock()
    kernel.broker = MagicMock()
    kernel.broker.position_qty = AsyncMock(return_value=5.0)
    audit_writer = MagicMock()
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_mode="cross_strategy",
        audit_writer=audit_writer,
    )

    ok, reason, meta = await svc._enforce_entry_isolation(strategy_id="s1", symbol="AAPL")

    assert ok is False
    assert reason == "entry_isolation_blocked_live_position"
    assert meta["broker_qty"] == 5.0
    blocked = [
        call.kwargs
        for call in audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "entry_isolation_blocked_live_position"
    ]
    assert len(blocked) == 1
    assert blocked[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_option_same_symbol_cross_strategy_can_override_to_strategy_only() -> None:
    conn = _FakeConn(
        pending_row_cross={"strategy_id": "other_strategy", "n": 2},
        pending_row_same=None,
    )
    pg = _FakePg(conn)
    kernel = MagicMock()
    kernel.broker = MagicMock()
    kernel.broker.position_qty = AsyncMock(return_value=8.0)
    svc = ExecutionService(
        pg=pg,
        risk_service=MagicMock(),
        safety_kernel=kernel,
        enable_execution=True,
        entry_isolation_enabled=True,
        entry_isolation_mode="cross_strategy",
    )

    mode = svc._effective_entry_isolation_mode(
        configured_mode="cross_strategy",
        symbol="OPT:QQQ:20260326:C:530",
        envelope_extras={
            "instrument_type": "OPTION",
            "allow_same_option_symbol_cross_strategy": True,
        },
    )
    ok, reason, meta = await svc._enforce_entry_isolation(
        strategy_id="s1",
        symbol="OPT:QQQ:20260326:C:530",
        entry_isolation_mode_override=mode,
    )
    assert mode == "strategy_only"
    assert ok is True
    assert reason == "ok"
    assert meta == {}
    assert kernel.broker.position_qty.await_count == 0
