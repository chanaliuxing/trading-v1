from datetime import datetime, timezone
import json

import pytest

from services.exec.models import Fill
from services.reconciliation.broker_truth_convergence import BrokerTruthConvergenceService


ORDER_TS = datetime(2026, 5, 2, 14, 29, tzinfo=timezone.utc)


def _candidate_order(**overrides):
    row = {
        "order_id": "order-1",
        "account_id": "DU123",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "SELL",
        "qty": 106,
        "idempotency_key": "idem-1",
        "order_ref": "ref-1",
        "state": "SUBMITTED",
        "created_at": ORDER_TS,
        "submitted_at": ORDER_TS,
        "updated_at": ORDER_TS,
        "note": None,
    }
    row.update(overrides)
    return row


class _FakePG:
    def __init__(self):
        self.fetchone_calls = []
        self.execute_calls = []

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "1"

    async def fetchone(self, sql, *args):
        self.fetchone_calls.append((sql, args))
        if "COUNT(*)::int AS event_count" in sql:
            return {
                "event_count": 1,
                "max_created_at": datetime(2026, 5, 2, 14, 31, tzinfo=timezone.utc),
                "max_event_ts": datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc),
                "net_qty": 106,
            }
        if "INSERT INTO core.core_order" in sql:
            return {
                "order_id": "adopted-order-1",
                "strategy_id": "adopted_reconciler",
                "symbol": "TQQQ",
                "idempotency_key": "broker_truth:fill:exec-1",
                "order_ref": "ref-1",
            }
        if "INSERT INTO core.core_fill" in sql:
            return {"inserted": 1}
        if "INSERT INTO core.position_ledger_event" in sql:
            return {"ledger_event_id": "ledger-1"}
        return None

    async def fetchall(self, sql, *args):
        self.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return []
        return []


@pytest.mark.asyncio
async def test_converge_unattributed_fill_quarantines_when_account_unknown(monkeypatch) -> None:
    monkeypatch.delenv("IB_ACCOUNT", raising=False)
    pg = _FakePG()
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-unknown:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-unknown",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "account_identity_unavailable"
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)
    assert not any("INSERT INTO core.position_ledger_event" in sql for sql, _args in pg.fetchone_calls)
    assert any("INSERT INTO core.core_order_quarantine" in sql for sql, _args in pg.execute_calls)
    assert any("INSERT INTO core.fill_attribution_ledger" in sql for sql, _args in pg.execute_calls)


@pytest.mark.asyncio
async def test_converge_unattributed_fill_quarantines_when_no_order_identity_match(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-1:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-1",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "unattributed_fill_quarantined"
    assert not any("INSERT INTO core.core_order" in sql for sql, _args in pg.fetchone_calls)
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)
    assert not any("INSERT INTO core.position_ledger_event" in sql for sql, _args in pg.fetchone_calls)
    assert any("INSERT INTO core.core_order_quarantine" in sql for sql, _args in pg.execute_calls)
    assert any("INSERT INTO core.fill_attribution_ledger" in sql for sql, _args in pg.execute_calls)


@pytest.mark.asyncio
async def test_converge_unattributed_fill_without_owner_identity_goes_unknown_owner(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchone(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "INSERT INTO core.core_fill" in sql:
            return {"inserted": 1}
        if "INSERT INTO core.position_ledger_event" in sql:
            return {"ledger_event_id": "ledger-1"}
        return None

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return [
                _candidate_order()
            ]
        return []

    pg.fetchone = _fetchone
    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-1:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-1",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "owner_identity_incomplete"
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)
    assert not any("INSERT INTO core.position_ledger_event" in sql for sql, _args in pg.fetchone_calls)
    candidate_call = next((sql, args) for sql, args in pg.fetchone_calls if "FROM core.core_order" in sql)
    assert candidate_call[1][2] == "DU123"
    quarantine_call = next((sql, args) for sql, args in pg.execute_calls if "INSERT INTO core.core_order_quarantine" in sql)
    assert quarantine_call[1][-1] == "owner_identity_incomplete"
    ledger_attr_call = next((sql, args) for sql, args in pg.execute_calls if "INSERT INTO core.fill_attribution_ledger" in sql)
    assert ledger_attr_call[1][12] == "unknown"
    assert ledger_attr_call[1][18] is False
    assert ledger_attr_call[1][19] is False
    assert ledger_attr_call[1][20] is True


@pytest.mark.asyncio
async def test_converge_unattributed_fill_inherits_owner_lane_from_order_note(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchone(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "INSERT INTO core.core_fill" in sql:
            return {"inserted": 1}
        if "INSERT INTO core.position_ledger_event" in sql:
            return {"ledger_event_id": "ledger-1"}
        return None

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return [
                _candidate_order(
                    note=json.dumps(
                        {
                            "extras": {
                                "strategy_instance_id": "qqq_intraday_restart_001",
                                "position_lot_id": "lot-restart-1",
                                "position_owner_key": "qqq_intraday_001:qqq_intraday_restart_001:TQQQ:lot-restart-1",
                                "exit_order_role": "FLATTEN",
                            }
                        }
                    )
                )
            ]
        return []

    pg.fetchone = _fetchone
    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-owner:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-owner",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is True
    ledger_call = next((sql, args) for sql, args in pg.fetchone_calls if "INSERT INTO core.position_ledger_event" in sql)
    assert ledger_call[1][3] == "lot-restart-1"
    assert ledger_call[1][16] == "qqq_intraday_restart_001"
    assert ledger_call[1][17] == "qqq_intraday_001:qqq_intraday_restart_001:TQQQ:lot-restart-1"
    assert ledger_call[1][19] == "FLATTEN"


@pytest.mark.asyncio
async def test_converge_unattributed_fill_quarantines_ambiguous_order_identity(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return [
                _candidate_order(),
                _candidate_order(
                    order_id="order-2",
                    strategy_id="nvda_minute_flip_paper_001",
                    idempotency_key="idem-2",
                ),
            ]
        return []

    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-1:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-1",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "ambiguous_fill_quarantined"
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)
    assert not any("INSERT INTO core.position_ledger_event" in sql for sql, _args in pg.fetchone_calls)
    quarantine_call = next((sql, args) for sql, args in pg.execute_calls if "INSERT INTO core.core_order_quarantine" in sql)
    assert quarantine_call[1][-1] == "ambiguous_broker_fill"
    ledger_attr_call = next((sql, args) for sql, args in pg.execute_calls if "INSERT INTO core.fill_attribution_ledger" in sql)
    assert ledger_attr_call[1][12] == "ambiguous"
    assert ledger_attr_call[1][16] == 2
    assert ledger_attr_call[1][18] is False
    assert ledger_attr_call[1][20] is True


@pytest.mark.asyncio
async def test_converge_unattributed_fill_quarantines_weak_symbol_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return [
                _candidate_order(
                    strategy_id="nvda_minute_flip_paper_001",
                    symbol="NVDA",
                )
            ]
        return []

    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-1:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-1",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "ambiguous_fill_quarantined"
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)


@pytest.mark.asyncio
async def test_converge_unattributed_fill_quarantines_stale_order_identity(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            stale_ts = datetime(2026, 4, 29, 14, 29, tzinfo=timezone.utc)
            return [_candidate_order(created_at=stale_ts, submitted_at=stale_ts, updated_at=stale_ts)]
        return []

    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-1:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-1",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "ambiguous_fill_quarantined"
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)


@pytest.mark.asyncio
async def test_converge_unattributed_option_fill_requires_exact_contract_symbol(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return [
                _candidate_order(
                    symbol="OPT:QQQ:20260501:675:C",
                    strategy_id="qqq_intraday_option_v1_001",
                    qty=2,
                    order_ref="opt-ref-1",
                )
            ]
        return []

    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-opt:309",
        order_id=309,
        order_ref="opt-ref-1",
        symbol="OPT:QQQ:20260429:675:C",
        qty=2,
        price=1.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-opt",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "ambiguous_fill_quarantined"
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)


@pytest.mark.asyncio
async def test_polluted_tqqq_soxl_nvda_fill_fixture_does_not_mutate_ledger(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()

    async def _fetchall(sql, *args):
        pg.fetchone_calls.append((sql, args))
        if "SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref" in sql:
            return [
                _candidate_order(symbol="NVDA", strategy_id="nvda_minute_flip_paper_001"),
                _candidate_order(symbol="SOXL", side="BUY", strategy_id="soxl_recovery_001"),
                _candidate_order(
                    symbol="TQQQ",
                    strategy_id="tqqq_old_recovery_001",
                    created_at=datetime(2026, 4, 29, 14, 29, tzinfo=timezone.utc),
                    submitted_at=datetime(2026, 4, 29, 14, 29, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 4, 29, 14, 29, tzinfo=timezone.utc),
                ),
            ]
        return []

    pg.fetchall = _fetchall
    svc = BrokerTruthConvergenceService(pg=pg)
    fill = Fill(
        fill_id="exec-polluted:309",
        order_id=309,
        order_ref="ref-1",
        symbol="TQQQ",
        qty=106,
        price=50.25,
        ts_us=int(datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1e6),
        exec_id="exec-polluted",
        perm_id=12345,
    )

    result = await svc.converge_unattributed_fill(fill=fill, side="SELL")

    assert result.ok is False
    assert result.reason == "ambiguous_fill_quarantined"
    assert result.metadata["candidate_count"] == 3
    assert not any("INSERT INTO core.core_fill" in sql for sql, _args in pg.fetchone_calls)
    assert not any("INSERT INTO core.position_ledger_event" in sql for sql, _args in pg.fetchone_calls)
    ledger_attr_call = next((sql, args) for sql, args in pg.execute_calls if "INSERT INTO core.fill_attribution_ledger" in sql)
    assert ledger_attr_call[1][12] == "ambiguous"
    assert ledger_attr_call[1][18] is False
    assert ledger_attr_call[1][19] is False


@pytest.mark.asyncio
async def test_converge_position_mismatch_appends_adjustment(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()
    svc = BrokerTruthConvergenceService(pg=pg)

    result = await svc.converge_position_mismatch(
        symbol="TQQQ",
        ib_qty=0.0,
        ledger_qty=106.0,
        strategy_id="qqq_intraday_001",
    )

    assert result.ok is True
    assert result.reason == "position_mismatch_adjusted"
    ledger_call = next((sql, args) for sql, args in pg.fetchone_calls if "INSERT INTO core.position_ledger_event" in sql)
    assert ledger_call[1][2] == "TQQQ"
    assert ledger_call[1][5] == "SELL"
    assert ledger_call[1][6] == -106.0
    assert ledger_call[1][8].startswith("broker_truth:position:DU123:qqq_intraday_001:TQQQ:0.00000000:106.00000000:")
    assert "events=1" in ledger_call[1][8]


@pytest.mark.asyncio
async def test_position_adjustment_idempotency_includes_ledger_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")
    pg = _FakePG()
    svc = BrokerTruthConvergenceService(pg=pg)

    key_a = svc._position_adjustment_idempotency_key(
        strategy_id="s1",
        symbol="NVDA",
        ib_qty=0.0,
        ledger_qty=1.0,
        ledger_snapshot_token="events=10:created=2026-05-05T03:18:22+00:00:net=1.00000000",
    )
    key_b = svc._position_adjustment_idempotency_key(
        strategy_id="s1",
        symbol="NVDA",
        ib_qty=0.0,
        ledger_qty=1.0,
        ledger_snapshot_token="events=12:created=2026-05-05T03:51:29+00:00:net=1.00000000",
    )

    assert key_a != key_b


@pytest.mark.asyncio
async def test_converge_broker_flat_lots_preserves_owner_lane(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class FakePG(_FakePG):
        async def fetchall(self, sql, *args):
            self.fetchone_calls.append((sql, args))
            if "FROM core.position_ledger_current" in sql:
                return [
                    {
                        "lot_id": "lot-1",
                        "strategy_instance_id": "strategy-instance-1",
                        "position_owner_key": "strategy:strategy-instance-1:NVDA:lot-1",
                        "qty": 1.0,
                    }
                ]
            return []

    pg = FakePG()
    svc = BrokerTruthConvergenceService(pg=pg)

    result = await svc.converge_position_mismatch(
        symbol="NVDA",
        ib_qty=0.0,
        ledger_qty=1.0,
        strategy_id="strategy",
    )

    assert result.ok is True
    assert result.reason == "position_lots_adjusted_to_broker_flat"
    ledger_call = next((sql, args) for sql, args in pg.fetchone_calls if "INSERT INTO core.position_ledger_event" in sql)
    assert ledger_call[1][3] == "lot-1"
    assert ledger_call[1][8].startswith(
        "broker_truth:position_lot_flat:DU123:strategy:NVDA:strategy-instance-1:lot-1:"
    )
    assert ledger_call[1][11] == "strategy-instance-1"
    assert ledger_call[1][12] == "strategy:strategy-instance-1:NVDA:lot-1"
