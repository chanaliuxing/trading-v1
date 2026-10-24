from datetime import datetime
from pathlib import Path

import pytest

from services.orders.ledger import PositionLedger


class _FakePG:
    def __init__(self):
        self.execute_calls = []
        self.fetchone_calls = []

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))

    async def fetchone(self, sql, *args):
        self.fetchone_calls.append((sql, args))
        return {"ledger_event_id": "event-1"}

    async def fetchall(self, sql, *args):
        self.fetchone_calls.append((sql, args))
        return []


class _SchemaPG(_FakePG):
    async def fetchone(self, sql, *args):
        self.fetchone_calls.append((sql, args))
        return {"deleted_count": 2}


@pytest.mark.asyncio
async def test_position_ledger_append_fill_event_writes_signed_qty(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()
    ledger = PositionLedger(pg=pg)

    inserted = await ledger.append_fill_event(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        qty=25,
        price=50.5,
        event_ts=datetime(2026, 4, 21, 14, 30),
        commission=1.25,
        broker_order_id=1001,
        ib_perm_id=2002,
        exec_id="exec-1",
        idempotency_key="idem-1",
        order_ref="ref-1",
        lot_id="lot-1",
        metadata={"source": "test"},
    )

    assert inserted is True
    _, args = pg.fetchone_calls[0]
    assert args[0] == "DU123"
    assert args[1] == "qqq_intraday_001"
    assert args[2] == "TQQQ"
    assert args[3] == "lot-1"
    assert args[5] == "SELL"
    assert args[6] == -25.0
    assert args[7] == 50.5


@pytest.mark.asyncio
async def test_position_ledger_schema_dedupes_before_unique_indexes(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _SchemaPG()
    ledger = PositionLedger(pg=pg)

    await ledger.ensure_schema()

    assert len(pg.fetchone_calls) == 2
    dedupe_sql = pg.fetchone_calls[0][0]
    assert "ROW_NUMBER() OVER" in dedupe_sql
    assert "idempotency_key" in dedupe_sql
    first_unique_idx = next(
        i for i, (sql, _args) in enumerate(pg.execute_calls) if "uq_position_ledger_exec" in sql
    )
    assert first_unique_idx > 0
    view_idx = next(
        i for i, (sql, _args) in enumerate(pg.execute_calls) if sql.startswith("CREATE OR REPLACE VIEW core.position_ledger_current")
    )
    assert pg.execute_calls[view_idx - 1][0] == "DROP VIEW IF EXISTS core.position_ledger_current"


@pytest.mark.asyncio
async def test_position_ledger_schema_bootstraps_empty_current_state(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _BootstrapPG(_FakePG):
        def __init__(self):
            super().__init__()
            self.fetchone_index = 0

        async def fetchone(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            self.fetchone_index += 1
            if "duplicate_rows" in sql:
                return {"deleted_count": 0}
            if "position_ledger_current_state" in sql and "event_count" in sql:
                return {"current_count": 0, "event_count": 2}
            return {}

        async def fetchall(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            if "SELECT DISTINCT account_id" in sql:
                return [{"account_id": "DU123", "strategy_id": "s1", "symbol": "QQQ", "lot_id": "lot-1"}]
            return [
                {
                    "event_ts": datetime(2026, 5, 5, 10, 0),
                    "qty_delta": 2,
                    "price": 100.0,
                    "commission": 0.0,
                    "fees": 0.0,
                }
            ]

    pg = _BootstrapPG()
    ledger = PositionLedger(pg=pg)

    await ledger.ensure_schema()

    assert any("SELECT DISTINCT account_id" in sql for sql, _args in pg.fetchone_calls)
    assert any("INSERT INTO core.position_ledger_current_state" in sql for sql, _args in pg.execute_calls)


def test_position_ledger_current_view_reads_bounded_projection() -> None:
    ddl = Path("db/pg/migration_20260421_uom_position_ledger.ddl").read_text(encoding="utf-8")

    view_sql = ddl.split("CREATE OR REPLACE VIEW core.position_ledger_current AS", 1)[1]
    assert "CREATE TABLE IF NOT EXISTS core.position_ledger_current_state" in ddl
    assert "WITH RECURSIVE" not in view_sql
    assert "FROM core.position_ledger_current_state" in view_sql
    assert "realized_pnl" in view_sql
    assert "ELSE SUM(qty_delta * price)" not in view_sql


def test_position_ledger_schema_blocks_new_unknown_account_rows() -> None:
    ddl = Path("db/pg/migration_20260421_uom_position_ledger.ddl").read_text(encoding="utf-8")

    assert "core_protection_order_registry_account_known_chk" in ddl
    assert "position_ledger_event_account_known_chk" in ddl
    assert "position_ledger_current_state_account_known_chk" in ddl
    assert "upper(btrim(account_id)) <> 'UNKNOWN_ACCOUNT'" in ddl
    assert ddl.count("NOT VALID") >= 3


def test_position_ledger_schema_splitter_keeps_do_blocks_intact() -> None:
    ddl = Path("db/pg/migration_20260421_uom_position_ledger.ddl").read_text(encoding="utf-8")

    statements = PositionLedger._split_ddl_statements(ddl)

    account_constraint_blocks = [
        stmt for stmt in statements if stmt.startswith("DO $$") and "_account_known_chk" in stmt
    ]
    assert len(account_constraint_blocks) == 3
    assert all("END $$" in stmt for stmt in account_constraint_blocks)
    assert not any(stmt.strip().startswith("END $$") for stmt in statements)


@pytest.mark.asyncio
async def test_position_ledger_runtime_rejects_unknown_account(monkeypatch) -> None:
    monkeypatch.delenv("IB_ACCOUNT", raising=False)
    pg = _FakePG()
    ledger = PositionLedger(pg=pg)

    inserted = await ledger.append_fill_event(
        strategy_id="s1",
        symbol="TQQQ",
        side="BUY",
        qty=1,
        price=100.0,
        event_ts=datetime(2026, 5, 5, 10, 0),
        lot_id="lot-1",
    )

    assert inserted is False
    assert pg.fetchone_calls == []
    assert pg.execute_calls == []


@pytest.mark.asyncio
async def test_position_ledger_append_fill_event_refreshes_current_state_lane(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _RefreshPG(_FakePG):
        async def fetchall(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            return [
                {
                    "event_ts": datetime(2026, 5, 5, 10, 0),
                    "qty_delta": 10,
                    "price": 100.0,
                    "commission": 1.0,
                    "fees": 0.25,
                },
                {
                    "event_ts": datetime(2026, 5, 5, 10, 5),
                    "qty_delta": -5,
                    "price": 110.0,
                    "commission": 0.5,
                    "fees": 0.1,
                },
            ]

    pg = _RefreshPG()
    ledger = PositionLedger(pg=pg)

    inserted = await ledger.append_fill_event(
        strategy_id="s1",
        symbol="qqq",
        side="BUY",
        qty=10,
        price=100.0,
        event_ts=datetime(2026, 5, 5, 10, 0),
        lot_id="lot-1",
    )

    assert inserted is True
    state_sql, state_args = pg.execute_calls[-1]
    assert "INSERT INTO core.position_ledger_current_state" in state_sql
    assert state_args[0] == "DU123"
    assert state_args[1] == "s1"
    assert state_args[2] == "QQQ"
    assert state_args[3] == "lot-1"
    assert state_args[4] == "lot-1"
    assert state_args[5] == 5.0
    assert state_args[6] == 500.0
    assert state_args[7] == 100.0
    assert state_args[8] == 50.0


@pytest.mark.asyncio
async def test_position_ledger_current_position_reads_lot(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _CurrentPG(_FakePG):
        async def fetchall(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            return [
                {"event_ts": datetime(2026, 5, 5, 10, 0), "qty_delta": 10, "price": 42.5, "commission": 1.0, "fees": 0.25}
            ]

    pg = _CurrentPG()
    ledger = PositionLedger(pg=pg)

    current = await ledger.current_position(strategy_id="s1", symbol="qqq", lot_id="lot-1")

    assert current["qty"] == 10.0
    assert current["avg_px"] == 42.5
    assert current["realized_pnl"] == 0.0
    _, args = pg.fetchone_calls[0]
    assert args[0] == "DU123"
    assert args[1] == "s1"
    assert args[2] == "QQQ"
    assert args[3] == "lot-1"


@pytest.mark.asyncio
async def test_position_ledger_current_position_keeps_wac_after_reduction(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _CurrentPG(_FakePG):
        async def fetchall(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            return [
                {"event_ts": datetime(2026, 5, 5, 10, 0), "qty_delta": 10, "price": 100.0, "commission": 0.0, "fees": 0.0},
                {"event_ts": datetime(2026, 5, 5, 10, 5), "qty_delta": -5, "price": 110.0, "commission": 0.0, "fees": 0.0},
            ]

    ledger = PositionLedger(pg=_CurrentPG())

    current = await ledger.current_position(strategy_id="s1", symbol="qqq")

    assert current["qty"] == 5.0
    assert current["avg_px"] == 100.0
    assert current["realized_pnl"] == 50.0


@pytest.mark.asyncio
async def test_position_ledger_append_adjustment_event_is_idempotent_shape(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()
    ledger = PositionLedger(pg=pg)

    inserted = await ledger.append_adjustment_event(
        strategy_id="adopted_reconciler",
        symbol="tqqq",
        qty_delta=-106,
        event_ts=datetime(2026, 5, 2, 14, 30),
        idempotency_key="broker_truth:position:DU123:TQQQ:0:106",
        metadata={"reason": "broker_flat"},
    )

    assert inserted is True
    sql, args = pg.fetchone_calls[0]
    assert "ADJUSTMENT" in sql
    assert args[0] == "DU123"
    assert args[1] == "adopted_reconciler"
    assert args[2] == "TQQQ"
    assert args[5] == "SELL"
    assert args[6] == -106.0
    assert args[8] == "broker_truth:position:DU123:TQQQ:0:106"
