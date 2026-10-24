from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.execution_service import ExecutionResult
from services.telegram.manual_flatten_service import ManualFlattenService


def _ledger_row(
    strategy_id: str,
    symbol: str,
    qty: float,
    avg_px: float = 48.0,
    *,
    account_id: str = "DUO112752",
    strategy_instance_id: str | None = None,
    lot_id: str | None = None,
) -> dict:
    instance = strategy_instance_id or f"{strategy_id}_inst"
    return {
        "account_id": account_id,
        "strategy_id": strategy_id,
        "strategy_instance_id": instance,
        "symbol": symbol,
        "lot_id": lot_id or f"lot:{account_id}:{instance}:{symbol}",
        "qty": qty,
        "avg_px": avg_px,
    }


def test_manual_flatten_watchdog_enabled_by_default(monkeypatch):
    monkeypatch.delenv("MANUAL_FLATTEN_WATCHDOG_ENABLED", raising=False)
    svc = ManualFlattenService(
        pg=MagicMock(),
        execution_service=MagicMock(),
        ib_adapter=MagicMock(),
        runtime_reconciler=None,
        order_event_enabled=False,
    )

    assert svc._enable_flatten_watchdog is True


def test_manual_flatten_low_price_option_limit_does_not_cross_extra_tick():
    sell_limit = ManualFlattenService._calc_aggressive_limit(side="SELL", ref_px=0.02, offset_bps=1000.0)
    buy_limit = ManualFlattenService._calc_aggressive_limit(side="BUY", ref_px=0.02, offset_bps=1000.0)

    assert sell_limit == 0.02
    assert buy_limit == 0.02


@pytest.mark.asyncio
async def test_manual_flatten_option_overnight_waits_for_market_reopen(monkeypatch):
    svc = ManualFlattenService(
        pg=MagicMock(),
        execution_service=MagicMock(),
        ib_adapter=MagicMock(),
        runtime_reconciler=None,
        order_event_enabled=False,
    )
    svc._now_et = lambda: datetime(2026, 5, 6, 21, 0, tzinfo=ZoneInfo("America/New_York"))

    spec = await svc._build_order_spec(
        symbol="OPT:NVDA:20260506:207.5:P",
        side="SELL",
        avg_px_hint=2.0,
    )

    assert spec["error"] == "manual_flatten_awaiting_option_market_reopen"
    assert spec["session"] == "overnight"


@pytest.mark.asyncio
async def test_manual_flatten_no_targets_requires_db_and_broker_truth() -> None:
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.execute = AsyncMock(return_value="OK")
    execution_service = MagicMock()
    execution_service.execute = AsyncMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=False,
    )

    out = await svc.execute_flatten_all(trigger_id="telegram:truth-ok")

    assert out.total_orders == 0
    assert out.verification_state == "NO_TARGETS"
    assert out.verified_flat is True
    assert out.db_position_query_ok is True
    assert out.broker_position_query_ok is True


@pytest.mark.asyncio
async def test_manual_flatten_db_truth_failure_is_not_verified_flat() -> None:
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=RuntimeError("pg down"))
    pg.execute = AsyncMock(return_value="OK")
    execution_service = MagicMock()
    execution_service.execute = AsyncMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=False,
    )

    out = await svc.execute_flatten_all(trigger_id="telegram:truth-db-down")

    assert out.total_orders == 0
    assert out.verification_state == "POSITION_TRUTH_UNAVAILABLE"
    assert out.verified_flat is False
    assert out.db_position_query_ok is False
    assert any("db_position_query_failed" in item for item in out.truth_query_errors)


@pytest.mark.asyncio
async def test_manual_flatten_broker_truth_failure_is_not_verified_flat() -> None:
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.execute = AsyncMock(return_value="OK")
    execution_service = MagicMock()
    execution_service.execute = AsyncMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=MagicMock(side_effect=RuntimeError("ib down"))))
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=False,
    )

    out = await svc.execute_flatten_all(trigger_id="telegram:truth-ib-down")

    assert out.total_orders == 0
    assert out.verification_state == "POSITION_TRUTH_UNAVAILABLE"
    assert out.verified_flat is False
    assert out.broker_position_query_ok is False
    assert any("broker_position_query_failed" in item for item in out.truth_query_errors)


@pytest.mark.asyncio
async def test_manual_flatten_uses_db_positions_as_strategy_targets():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 120.0, 48.0, strategy_instance_id="qqq_intraday_001_a"),
        _ledger_row("index_super_win_001", "TQQQ", 80.0, 48.1, strategy_instance_id="index_super_win_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=123, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_all(trigger_id="telegram:1:100")

    assert out.db_targets == 2
    assert out.broker_fallback_targets == 0
    assert out.total_orders == 2
    assert execution_service.execute.await_count == 2
    first_call = execution_service.execute.call_args_list[0].args[0]
    assert first_call.strategy_id in {"qqq_intraday_001", "index_super_win_001"}
    assert first_call.plan is not None
    assert bool(first_call.metadata.get("envelope_extras", {}).get("flatten_action")) is True
    assert bool(first_call.metadata.get("envelope_extras", {}).get("close_only")) is True
    assert bool(first_call.metadata.get("envelope_extras", {}).get("reduce_only")) is True
    assert first_call.metadata.get("envelope_extras", {}).get("order_role") == "STRATEGY_FLATTEN"


@pytest.mark.asyncio
async def test_manual_flatten_for_strategy_targets_only_one_strategy():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 120.0, 48.0, strategy_instance_id="qqq_intraday_001_a"),
        _ledger_row("index_super_win_001", "TQQQ", 80.0, 48.1, strategy_instance_id="index_super_win_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=123, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_for_strategy(trigger_id="telegram:1:110", strategy_id="qqq_intraday_001")

    assert out.db_targets == 1
    assert out.broker_fallback_targets == 0
    assert out.total_orders == 1
    call = execution_service.execute.call_args_list[0].args[0]
    assert call.strategy_id == "qqq_intraday_001"


@pytest.mark.asyncio
async def test_manual_flatten_adds_broker_fallback_for_untracked_symbol():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 48.0, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=123, stage="completed")
    )
    broker_positions = [
        SimpleNamespace(account="DUO112752", contract=SimpleNamespace(symbol="TQQQ"), position=100.0),
        SimpleNamespace(account="DUO112752", contract=SimpleNamespace(symbol="SQQQ"), position=-50.0),
    ]
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: broker_positions))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
        fallback_strategy_id="manual_telegram_flatten",
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_all(trigger_id="telegram:1:100")

    assert out.db_targets == 1
    assert out.broker_fallback_targets == 1
    assert out.total_orders == 2
    assert execution_service.execute.await_count == 2
    second_call = execution_service.execute.call_args_list[1].args[0]
    assert second_call.strategy_id == "manual_telegram_flatten"
    assert second_call.symbol == "SQQQ"
    assert second_call.plan.side == "BUY"


@pytest.mark.asyncio
async def test_manual_flatten_for_source_trigger_filters_strategy_symbol():
    pg = MagicMock()
    async def _fetchall(sql: str, *params):
        if "FROM core.position_ledger_current" in sql:
            return [
                _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 48.0, strategy_instance_id="qqq_intraday_001_a"),
                _ledger_row("index_super_win_001", "TQQQ", 80.0, 48.1, strategy_instance_id="index_super_win_001_a"),
            ]
        if "FROM core.core_order o" in sql:
            return [{"strategy_id": "index_super_win_001", "symbol": "TQQQ", "signed_fill_qty": 80.0}]
        return []
    pg.fetchall = AsyncMock(side_effect=_fetchall)
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=123, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_for_source_trigger(
        trigger_id="telegram:1:111",
        source_trigger_id="src-trigger-1",
    )

    assert out.db_targets == 1
    assert out.scope_mode == "TRIGGER_NET_CAPPED"
    assert "not lot-exact" in out.scope_warning
    assert out.total_orders == 1
    call = execution_service.execute.call_args_list[0].args[0]
    assert call.strategy_id == "index_super_win_001"
    extras = call.metadata.get("envelope_extras", {})
    assert extras.get("trigger_id") == "telegram:1:111"
    assert extras.get("source_trigger_id") == "src-trigger-1"
    assert extras.get("manual_trigger_id") == "telegram:1:111"


@pytest.mark.asyncio
async def test_manual_flatten_for_source_trigger_caps_to_trigger_net_qty():
    pg = MagicMock()

    async def _fetchall(sql: str, *params):
        if "FROM core.position_ledger_current" in sql:
            return [_ledger_row("qqq_intraday_001", "TQQQ", 100.0, 48.0, strategy_instance_id="qqq_intraday_001_a")]
        if "FROM core.core_order o" in sql:
            return [{"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "signed_fill_qty": 35.0}]
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=124, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_for_source_trigger(
        trigger_id="telegram:1:cap",
        source_trigger_id="src-trigger-cap",
    )

    assert out.db_targets == 1
    assert out.scope_mode == "TRIGGER_NET_CAPPED"
    call = execution_service.execute.call_args_list[0].args[0]
    assert float(call.plan.qty) == 35.0


@pytest.mark.asyncio
async def test_manual_flatten_for_source_trigger_skips_when_sign_mismatch():
    pg = MagicMock()

    async def _fetchall(sql: str, *params):
        if "FROM core.position_ledger_current" in sql:
            return [_ledger_row("qqq_intraday_001", "TQQQ", 100.0, 48.0, strategy_instance_id="qqq_intraday_001_a")]
        if "FROM core.core_order o" in sql:
            return [{"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "signed_fill_qty": -35.0}]
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_for_source_trigger(
        trigger_id="telegram:1:mismatch",
        source_trigger_id="src-trigger-mismatch",
    )

    assert out.db_targets == 0
    assert out.total_orders == 0
    assert execution_service.execute.await_count == 0


@pytest.mark.asyncio
async def test_manual_flatten_triggers_three_chain_contract():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 48.0, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=321, stage="completed")
    )
    reconciler = MagicMock()
    reconciler.run_once = AsyncMock(return_value=SimpleNamespace())
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=reconciler,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_all(trigger_id="telegram:1:888")

    # order chain: must go through ExecutionService
    assert execution_service.execute.await_count == 1
    # position chain: must trigger post-flatten reconcile
    assert reconciler.run_once.await_count == 1
    assert out.position_reconcile_ran is True
    assert out.verification_state == "VERIFIED_FLAT"
    assert out.verified_flat is True
    # event chain: intervention started/completed persisted
    event_sql_calls = [c for c in pg.execute.call_args_list if "INSERT INTO core.core_order_event" in str(c.args[0])]
    assert len(event_sql_calls) >= 2


@pytest.mark.asyncio
async def test_manual_flatten_overnight_uses_limit_order_and_audit_fields(monkeypatch):
    monkeypatch.setenv("MANUAL_FLATTEN_ALLOW_OVERNIGHT_ORDERS", "true")
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 47.5, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=222, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 21, 5, tzinfo=ZoneInfo("America/New_York"))
    svc._get_quote_snapshot = AsyncMock(return_value={"bid": 50.0, "ask": 50.1, "last": 50.05, "mid": 50.05})

    await svc.execute_flatten_all(trigger_id="telegram:1:999")

    call = execution_service.execute.call_args_list[0].args[0]
    assert call.plan.order_type == "LMT"
    assert call.plan.tif == "DAY"
    assert float(call.plan.limit_px) > 0
    extras = call.metadata.get("envelope_extras", {})
    assert extras.get("manual_session") == "overnight"
    assert extras.get("manual_order_type_selected") == "LMT"
    assert isinstance(extras.get("manual_quote_snapshot"), dict)
    assert bool(extras.get("outside_rth")) is True
    assert extras.get("exchange_override") == "OVERNIGHT"
    assert float(extras.get("manual_limit_guard_max_dev_bps") or 0.0) >= 1000.0
    assert float(extras.get("manual_ref_px") or 0) > 0
    assert str(extras.get("order_ref") or "").startswith("manual_flatten:")


@pytest.mark.asyncio
async def test_manual_flatten_watchdog_starts_only_on_success(monkeypatch):
    monkeypatch.setenv("MANUAL_FLATTEN_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("MANUAL_FLATTEN_ALLOW_OVERNIGHT_ORDERS", "true")
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 47.5, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=222, stage="completed", idempotency_key="idem-x")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 21, 5, tzinfo=ZoneInfo("America/New_York"))
    svc._get_quote_snapshot = AsyncMock(return_value={"bid": 50.0, "ask": 50.1, "last": 50.05, "mid": 50.05})
    started = []
    monkeypatch.setattr(
        svc,
        "_watch_flatten_until_terminal",
        AsyncMock(side_effect=lambda **kwargs: started.append(kwargs)),
    )

    await svc.execute_flatten_all(trigger_id="telegram:1:watch")
    assert len(svc._watchdog_tasks) == 1


@pytest.mark.asyncio
async def test_manual_flatten_rejects_overnight_without_ref_price():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 0.0, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=222, stage="completed", idempotency_key="idem-x")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    provider = SimpleNamespace(get_last_close=AsyncMock(return_value=None))
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
        last_close_provider=provider,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 21, 5, tzinfo=ZoneInfo("America/New_York"))
    svc._get_quote_snapshot = AsyncMock(return_value={})

    out = await svc.execute_flatten_all(trigger_id="telegram:1:no-ref")
    assert out.failed_orders == 1
    assert out.order_results[0].reason == "manual_flatten_no_ref_price"
    assert execution_service.execute.await_count == 0


@pytest.mark.asyncio
async def test_manual_flatten_overnight_uses_provider_ref_price_when_quote_missing(monkeypatch):
    monkeypatch.setenv("MANUAL_FLATTEN_ALLOW_OVERNIGHT_ORDERS", "true")
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 0.0, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=222, stage="completed", idempotency_key="idem-x")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    provider = SimpleNamespace(get_last_close=AsyncMock(return_value={"px": 49.5, "source": "test:last_close"}))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
        last_close_provider=provider,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 21, 5, tzinfo=ZoneInfo("America/New_York"))
    svc._get_quote_snapshot = AsyncMock(return_value={})

    out = await svc.execute_flatten_all(trigger_id="telegram:1:provider-ref")
    assert out.failed_orders == 0
    assert execution_service.execute.await_count == 1
    call = execution_service.execute.call_args_list[0].args[0]
    extras = call.metadata.get("envelope_extras", {})
    assert float(extras.get("manual_ref_px") or 0) == 49.5
    assert extras.get("manual_anchor_px_source") in {"test:last_close", "ch:bars_1m_close", "ch:bars_1d_close"}
    assert bool(extras.get("outside_rth")) is True
    assert extras.get("exchange_override") == "OVERNIGHT"


@pytest.mark.asyncio
async def test_manual_flatten_can_disable_overnight_orders(monkeypatch):
    monkeypatch.setenv("MANUAL_FLATTEN_ALLOW_OVERNIGHT_ORDERS", "false")
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 47.5, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=222, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 21, 5, tzinfo=ZoneInfo("America/New_York"))

    out = await svc.execute_flatten_all(trigger_id="telegram:1:overnight-default")

    assert out.failed_orders == 1
    assert out.remaining_position_targets == 1
    assert out.order_results[0].reason == "manual_flatten_awaiting_market_reopen"
    assert out.order_results[0].session == "overnight"
    assert execution_service.execute.await_count == 0


@pytest.mark.asyncio
async def test_manual_flatten_ref_price_prefers_live_quote_over_stale_anchor():
    pg = MagicMock()
    execution_service = MagicMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    provider = SimpleNamespace(get_last_close=AsyncMock(return_value={"px": 1414.0, "source": "test:stale_close"}))
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
        last_close_provider=provider,
    )

    ref = await svc._resolve_ref_px(
        symbol="NVDA",
        side="SELL",
        quote={"bid": 198.5, "ask": 198.7, "last": 198.6, "mid": 198.6},
        avg_px_hint=None,
    )

    assert ref["ref_px"] == 198.5
    assert ref["ref_px_source"] == "quote"
    assert ref["anchor_px"] is None
    assert ref["anchor_rejected_reason"] == "anchor_off_quote"


@pytest.mark.asyncio
async def test_manual_flatten_wait_broker_order_absent_treats_pending_cancel_as_open():
    pg = MagicMock()
    execution_service = MagicMock()
    adapter = SimpleNamespace(
        open_orders=MagicMock(
            return_value=[
                {
                    "broker_order_id": "510",
                    "symbol": "NVDA",
                    "status": "PendingCancel",
                }
            ]
        )
    )
    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._watchdog_check_sec = 0.001

    absent = await svc._wait_broker_order_absent(broker_order_id=510, symbol="NVDA", timeout_sec=0.002)

    assert absent is False


@pytest.mark.asyncio
async def test_manual_flatten_quote_snapshot_uses_async_tickers():
    pg = MagicMock()
    execution_service = MagicMock()
    adapter = SimpleNamespace()
    adapter._contract = AsyncMock(return_value=SimpleNamespace(symbol="TQQQ"))
    ticker = SimpleNamespace(bid=49.0, ask=49.1, last=49.05)
    adapter.ib = SimpleNamespace(
        reqTickers=MagicMock(side_effect=RuntimeError("sync path should not be used")),
        reqTickersAsync=AsyncMock(return_value=[ticker]),
    )

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    out = await svc._get_quote_snapshot("TQQQ")
    assert out["bid"] == 49.0
    assert out["ask"] == 49.1
    assert adapter.ib.reqTickersAsync.await_count == 1
    assert adapter.ib.reqTickers.call_count == 0


@pytest.mark.asyncio
async def test_manual_flatten_broker_fallback_maps_option_to_internal_symbol():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.execute = AsyncMock(return_value="OK")

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=555, stage="completed")
    )
    broker_positions = [
        SimpleNamespace(
            account="DUO112752",
            contract=SimpleNamespace(
                symbol="QQQ",
                secType="OPT",
                lastTradeDateOrContractMonth="20260311",
                strike=610.0,
                right="P",
            ),
            position=3.0,
            avgCost=2.13,
        )
    ]
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: broker_positions))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
        fallback_strategy_id="manual_telegram_flatten",
    )
    svc._now_et = lambda: datetime(2026, 3, 11, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    out = await svc.execute_flatten_all(trigger_id="telegram:1:opt-fallback")

    assert out.broker_fallback_targets == 1
    call = execution_service.execute.call_args_list[0].args[0]
    assert call.symbol == "OPT:QQQ:20260311:610:P"
    extras = call.metadata.get("envelope_extras", {})
    assert float(extras.get("position_qty_hint") or 0.0) == 3.0


@pytest.mark.asyncio
async def test_manual_flatten_marks_submitted_not_verified_when_residuals_remain():
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[
        _ledger_row("qqq_intraday_001", "TQQQ", 100.0, 48.0, strategy_instance_id="qqq_intraday_001_a"),
    ])
    pg.execute = AsyncMock(return_value="OK")
    pg.fetchone = AsyncMock(side_effect=[{"qty": 25.0}, {"qty": 0.0}, {"n": 1}])

    execution_service = MagicMock()
    execution_service.execute = AsyncMock(
        return_value=ExecutionResult(success=True, decision_id="d", order_id=123, stage="completed")
    )
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    svc = ManualFlattenService(
        pg=pg,
        execution_service=execution_service,
        ib_adapter=adapter,
        runtime_reconciler=None,
        order_event_enabled=True,
    )
    svc._now_et = lambda: datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))

    out = await svc.execute_flatten_all(trigger_id="telegram:1:residual")

    assert out.verification_state == "SUBMITTED_NOT_VERIFIED"
    assert out.verified_flat is False
    assert out.remaining_position_targets == 1
    assert out.remaining_open_orders == 1
