from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import OrderUpdate, PositionSnapshot
from services.decision.pipeline import PipelineResult
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import KernelFillReport
from strategies.stocks_kernel.types import HistoryBar
from strategies.stocks_kernel.types import PositionLot
from strategies.stocks_kernel.runtime.audit import query_audit_chain

ET = ZoneInfo("America/New_York")


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _Health:
    async def subscribe(self, strategy_id, strategy):
        return None


@pytest.fixture(autouse=True)
def _stub_system_health_subscription(monkeypatch):
    async def _subscribe(self):
        self._system_health_subscribed = True

    monkeypatch.setattr(ComposableStocksKernelStrategy, "subscribe_system_health", _subscribe)


@pytest.mark.asyncio
async def test_composable_strategy_subscribes_and_writes_audit(tmp_path):
    calls = {"features": None, "ticks": None}

    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            calls["features"] = (strategy_id, tuple(symbols), bar_sec)

        async def subscribe_tick_data(self, strategy_id, symbols):
            calls["ticks"] = (strategy_id, tuple(symbols))
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_002",
        strategy_run_id="run-1",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_002", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_002", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_002",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)

    assert calls["features"] == ("kernel_test_002", ("QQQ", "SQQQ", "TQQQ"), 5)
    assert calls["ticks"] == ("kernel_test_002", ("QQQ", "SQQQ", "TQQQ"))
    assert strategy.kernel_state["QQQ"]["entry_plan"] is not None
    assert strategy.kernel_state["QQQ"]["signal"].action == "enter"
    strategy_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_002", domain="strategy")
    market_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_002", domain="market")
    assert any(row["stage"] == "entry_plan_built" for row in strategy_rows)
    assert any(row["stage"] == "market_summary" for row in market_rows)
    assert any(row["stage"] == "feature_state_changed" for row in market_rows)


@pytest.mark.asyncio
async def test_composable_strategy_submits_intent_when_not_dry_run(tmp_path):
    submit_calls = []

    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        market_data_service = SimpleNamespace(ib_adapter=None)

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.decision_id, envelope.target_qty, dict(envelope.extras)))
            return {"ok": True, "broker_order_id": "201", "perm_id": 201}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_003",
        strategy_run_id="run-3",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_003", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_003", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_003", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_003",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)

    strategy_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_003", domain="strategy")
    assert len(submit_calls) == 1
    assert any(row["stage"] == "intent_requested" for row in strategy_rows)
    assert any(row["stage"] == "intent_submit_succeeded" for row in strategy_rows)
    assert submit_calls[0][1].startswith("kernel_test_003:TQQQ:")
    assert submit_calls[0][3]["order_ref"].startswith("stkv1:kernel_test_003:TQQQ:ENTRY")
    assert submit_calls[0][3]["position_lot_id"]
    events = strategy.execution.audit.list_events(strategy_id="kernel_test_003", symbol="TQQQ")
    assert any(event.stage == "entry_lot_opened" for event in events)
    assert any(event.stage == "order_acknowledged" for event in events)


@pytest.mark.asyncio
async def test_composable_strategy_skips_non_signal_symbol_submission(tmp_path):
    submit_calls = []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.decision_id, envelope.target_qty))
            return {"ok": True}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_non_signal_001",
        strategy_run_id="run-non-signal",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_non_signal_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_non_signal_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_non_signal_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_non_signal_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "TQQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 50.0,
            "session": "RTH",
            "vwap": 49.0,
            "planned_qty": 1,
            "trigger_id": "t-non-signal",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    strategy_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_non_signal_001", domain="strategy")
    assert submit_calls == []
    assert any(row["stage"] == "non_signal_symbol_skipped" for row in strategy_rows)


@pytest.mark.asyncio
async def test_composable_strategy_dedupes_idle_non_signal_symbol_audit(tmp_path):
    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            raise AssertionError("idle non-signal symbol should short-circuit before history load")

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_non_signal_dedupe_001",
        strategy_run_id="run-non-signal-dedupe",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_non_signal_dedupe_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_non_signal_dedupe_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_non_signal_dedupe_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    payload = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 101.0,
        "last_price": 50.0,
        "session": "RTH",
        "vwap": 49.0,
        "planned_qty": 1,
        "trigger_id": "t-non-signal-dedupe",
        "bars_1m": [
            HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
            for i in range(1, 16)
        ],
    }
    await strategy.on_features("TQQQ", dict(payload))
    await strategy.on_features("TQQQ", dict(payload, ts_epoch_s=105.0, now_epoch_s=106.0))

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    strategy_rows = query_audit_chain(
        root_dir=tmp_path,
        strategy_id="kernel_test_non_signal_dedupe_001",
        domain="strategy",
    )
    market_rows = query_audit_chain(
        root_dir=tmp_path,
        strategy_id="kernel_test_non_signal_dedupe_001",
        domain="market",
    )

    skip_rows = [row for row in strategy_rows if row["stage"] == "non_signal_symbol_skipped"]
    assert len(skip_rows) == 1
    assert market_rows == []


@pytest.mark.asyncio
async def test_composable_strategy_blocks_on_broker_live_position(tmp_path):
    submit_calls = []

    class _Adapter:
        async def position_qty(self, symbol):
            return 1.0 if symbol == "TQQQ" else 0.0

        async def open_orders(self, force_refresh=False):
            return []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=_Adapter())

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.decision_id, envelope.target_qty))
            return {"ok": True}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_isolation_001",
        strategy_run_id="run-isolation",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_isolation_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_isolation_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_isolation_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_isolation_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    strategy_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_isolation_001", domain="strategy")
    assert submit_calls == []
    assert strategy._startup_block_reason == "broker_live_position_present"
    assert any(row["stage"] == "startup_blocked" for row in strategy_rows)


@pytest.mark.asyncio
async def test_composable_strategy_quarantines_rejected_pipeline_result_and_kills_symbol(tmp_path):
    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            return PipelineResult(
                status="REJECTED",
                decision_id=envelope.decision_id,
                strategy_id=strategy_id,
                symbol=envelope.symbol,
                stage="execution",
                reasons=["entry_isolation_blocked_live_position"],
            )

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_reject_001",
        strategy_run_id="run-reject",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_reject_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_reject_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_reject_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_reject_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 250.0,
            "session": "RTH",
            "vwap": 249.0,
            "planned_qty": 1,
            "trigger_id": "reject-1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_reject_001", domain="strategy")
    lots = strategy.execution.ledger.list_lots(strategy_id="kernel_test_reject_001", symbol="SOXL", include_closed=True)
    assert any(row["stage"] == "intent_submit_rejected" for row in rows)
    assert any(row["stage"] == "symbol_kill_switch_activated" for row in rows)
    assert strategy._symbol_kill_switch["SOXL"] == "entry_isolation_blocked_live_position"
    assert lots and lots[0].state == "quarantined"


@pytest.mark.asyncio
async def test_composable_strategy_quarantines_failed_execution_result_metadata(tmp_path):
    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            return PipelineResult(
                status="APPROVED",
                decision_id=envelope.decision_id,
                strategy_id=strategy_id,
                symbol=envelope.symbol,
                stage="execution",
                metadata={"execution_result": {"success": False, "reason": "manual_intervention_lock_active"}},
            )

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_exec_reject_001",
        strategy_run_id="run-exec-reject",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_exec_reject_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_exec_reject_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_exec_reject_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_exec_reject_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 250.0,
            "session": "RTH",
            "vwap": 249.0,
            "planned_qty": 1,
            "trigger_id": "exec-reject-1",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_exec_reject_001", domain="strategy")
    lots = strategy.execution.ledger.list_lots(strategy_id="kernel_test_exec_reject_001", symbol="SOXL", include_closed=True)
    assert any(row["stage"] == "intent_submit_rejected" for row in rows)
    assert not any(row["stage"] == "intent_submit_succeeded" for row in rows)
    assert not any(row["stage"] == "order_transition" and row["payload"].get("event_type") == "order_accepted" for row in rows)
    assert lots and lots[0].state == "quarantined"


@pytest.mark.asyncio
async def test_composable_strategy_reaps_stale_pending_entry_before_new_signal(tmp_path):
    submit_calls = []

    class _Adapter:
        async def position_qty(self, symbol):
            return 0.0

        async def open_orders(self, force_refresh=False):
            return []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=_Adapter())

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append(dict(envelope.extras))
            return {"ok": True, "broker_order_id": "401", "perm_id": 401}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_stale_001",
        strategy_run_id="run-stale",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_stale_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_stale_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_stale_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_stale_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "SMH", "long_exec_symbol": "SOXL", "short_exec_symbol": "SOXS"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    stale_lot = strategy.execution.open_entry_lot(
        strategy_id="kernel_test_stale_001",
        symbol="SOXL",
        side="LONG",
        decision_id="old-decision",
        trigger_id="old-trigger",
    )
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=stale_lot.position_lot_id,
        updates={"entry_requested_ts_epoch_s": 50.0},
    )
    await strategy.on_features(
        "SMH",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 250.0,
            "session": "RTH",
            "vwap": 249.0,
            "planned_qty": 1,
            "trigger_id": "new-trigger",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_stale_001", domain="strategy")
    lots = strategy.execution.ledger.list_lots(strategy_id="kernel_test_stale_001", symbol="SOXL", include_closed=True)
    assert any(row["stage"] == "stale_pending_entry_quarantined" for row in rows)
    assert len(submit_calls) == 1
    assert any(lot.position_lot_id == stale_lot.position_lot_id and lot.state == "quarantined" for lot in lots)


@pytest.mark.asyncio
async def test_composable_strategy_accepts_datetime_feature_timestamps(tmp_path):
    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    now = datetime(2026, 3, 31, 12, 5, 52)
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_datetime_001",
        strategy_run_id="run-dt",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_datetime_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_datetime_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_datetime_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": now,
            "now_epoch_s": now,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t-dt",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy.kernel_state["QQQ"]["signal"].action == "enter"


@pytest.mark.asyncio
async def test_composable_strategy_falls_back_to_close_and_derived_session(tmp_path):
    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    now = datetime(2026, 3, 31, 12, 10, 0)
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_fallback_001",
        strategy_run_id="run-fallback",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_fallback_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_fallback_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_fallback_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": now,
            "now_epoch_s": now,
            "close": 500.0,
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t-fallback",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    state = strategy.kernel_state["QQQ"]
    assert state["gate_report"].blocked_by is None
    assert state["signal"].action == "enter"


@pytest.mark.asyncio
async def test_composable_strategy_accepts_datetime_ts_alias(tmp_path):
    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    ts = datetime(2026, 3, 31, 12, 16, 0)
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_ts_alias_001",
        strategy_run_id="run-ts-alias",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_ts_alias_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_ts_alias_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_ts_alias_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts": ts,
            "close": 500.0,
            "vwap": 499.0,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    state = strategy.kernel_state["QQQ"]
    assert state["gate_report"].blocked_by is None
    assert state["feature_context"].snapshot.session == "RTH"


@pytest.mark.asyncio
async def test_composable_strategy_reuses_history_within_same_minute(tmp_path):
    calls = {"history": 0}

    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            calls["history"] += 1
            return [
                {"ts_epoch_s": float(i), "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10 * i}
                for i in range(1, 16)
            ]

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_cache_001",
        strategy_run_id="run-cache",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_cache_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_cache_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        system_health=_Health(),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_cache_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts": datetime(2026, 3, 31, 12, 16, 1),
            "close": 568.06,
            "vwap": 568.13,
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "ts": datetime(2026, 3, 31, 12, 16, 40),
            "close": 568.10,
            "vwap": 568.14,
        },
    )

    assert calls["history"] == 1


@pytest.mark.asyncio
async def test_composable_strategy_shares_history_cache_across_instances(tmp_path):
    calls = {"history": 0}

    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            calls["history"] += 1
            return [
                {"ts_epoch_s": float(i), "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10 * i}
                for i in range(1, 16)
            ]

    engine = _Engine()

    def _ctx(strategy_id):
        return SimpleNamespace(
            log=_Logger(),
            metrics=SimpleNamespace(),
            engine=engine,
            strategy_id=strategy_id,
            strategy_run_id=f"run-{strategy_id}",
            subscribe_features=lambda symbols, bar_sec=5: engine.subscribe_features(strategy_id, symbols, bar_sec),
            subscribe_tick_data=lambda symbols: engine.subscribe_tick_data(strategy_id, symbols),
            history=lambda symbol, resolution, limit: engine.history_features(symbol, resolution, limit),
            system_health=_Health(),
        )

    def _params(strategy_id):
        return SimpleNamespace(
            strategy_id=strategy_id,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "audit_root_dir": str(tmp_path),
                "feature_set": ["vwap", "atr_eff", "v3m"],
                "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
                "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
                "enable_shared_history_cache": True,
            },
        )

    first = ComposableStocksKernelStrategy(ctx=_ctx("kernel_test_cache_a_001"), params=_params("kernel_test_cache_a_001"))
    second = ComposableStocksKernelStrategy(ctx=_ctx("kernel_test_cache_b_001"), params=_params("kernel_test_cache_b_001"))
    await first.initialize()
    await second.initialize()
    payload = {
        "ts": datetime(2026, 3, 31, 12, 16, 1),
        "close": 568.06,
        "vwap": 568.13,
        "session": "RTH",
    }
    await first.on_features("QQQ", dict(payload))
    await second.on_features("QQQ", dict(payload, close=568.07))

    assert calls["history"] == 1


@pytest.mark.asyncio
async def test_composable_strategy_startup_blocks_on_broker_live_position(tmp_path):
    class _Adapter:
        async def position_qty(self, symbol):
            return 1.0 if symbol == "TQQQ" else 0.0

        async def open_orders(self, force_refresh=False):
            return []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=_Adapter())

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_preflight_broker_pos_001",
        strategy_run_id="run-preflight-broker-pos",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_preflight_broker_pos_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_preflight_broker_pos_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_preflight_broker_pos_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_preflight_broker_pos_001", domain="strategy")
    assert strategy._startup_block_reason == "broker_live_position_present"
    assert any(row["stage"] == "startup_blocked" for row in rows)
    assert not any(row["stage"] == "startup_self_check" for row in rows)


@pytest.mark.asyncio
async def test_composable_strategy_startup_blocks_on_broker_open_orders(tmp_path):
    class _Order:
        symbol = "TQQQ"
        order_ref = "oref-1"

    class _Adapter:
        async def position_qty(self, symbol):
            return 0.0

        async def open_orders(self, force_refresh=False):
            return [_Order()]

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=_Adapter())

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_preflight_broker_order_001",
        strategy_run_id="run-preflight-broker-order",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_preflight_broker_order_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_preflight_broker_order_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_preflight_broker_order_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._startup_block_reason == "broker_open_orders_present"


@pytest.mark.asyncio
async def test_composable_strategy_startup_blocks_on_db_residuals(tmp_path):
    class _Pg:
        async def fetchall(self, query, *params):
            sql = " ".join(str(query).split())
            if "FROM core.core_position" in sql:
                return [{"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "pos_qty": 9.0}]
            if "FROM core.core_order" in sql:
                return [{"strategy_id": "stocks_kernel_shadow_qqq_001", "symbol": "TQQQ", "side": "SELL", "qty": 1, "state": "NEW", "broker_order_id": None}]
            return []

    class _Adapter:
        async def position_qty(self, symbol):
            return 0.0

        async def open_orders(self, force_refresh=False):
            return []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=_Adapter())
        pg = _Pg()

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_preflight_db_001",
        strategy_run_id="run-preflight-db",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_preflight_db_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_preflight_db_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_preflight_db_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    assert strategy._startup_block_reason == "db_residual_present"


@pytest.mark.asyncio
async def test_composable_strategy_startup_preflight_can_be_disabled(tmp_path):
    submit_calls = []

    class _Adapter:
        async def position_qty(self, symbol):
            return 1.0 if symbol == "TQQQ" else 0.0

        async def open_orders(self, force_refresh=False):
            return []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=_Adapter())

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.decision_id))
            return {"ok": True}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_preflight_disable_001",
        strategy_run_id="run-preflight-disable",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_preflight_disable_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_preflight_disable_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_preflight_disable_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_preflight_disable_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-preflight-disabled",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    assert strategy._startup_block_reason is None
    assert submit_calls == []


@pytest.mark.asyncio
async def test_composable_strategy_symbol_kill_switch_blocks_repeat_submit(tmp_path):
    submit_calls = []

    class _Adapter:
        def __init__(self):
            self.calls = 0

        async def position_qty(self, symbol):
            self.calls += 1
            return 1.0 if symbol == "TQQQ" else 0.0

        async def open_orders(self, force_refresh=False):
            return []

    adapter = _Adapter()

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=adapter)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.decision_id))
            return {"ok": True}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_kill_switch_001",
        strategy_run_id="run-kill-switch",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_kill_switch_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_kill_switch_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_kill_switch_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_kill_switch_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    feature_payload = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 101.0,
        "last_price": 500.0,
        "session": "RTH",
        "vwap": 499.0,
        "planned_qty": 1,
        "trigger_id": "t-kill-switch",
        "bars_1m": [
            HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
            for i in range(1, 16)
        ],
    }
    await strategy.on_features("QQQ", dict(feature_payload))
    await strategy.on_features("QQQ", dict(feature_payload, ts_epoch_s=105.0, now_epoch_s=106.0))

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_kill_switch_001", domain="strategy")
    assert submit_calls == []
    assert strategy._symbol_kill_switch["TQQQ"] == "broker_live_position_present"
    assert any(row["stage"] == "symbol_kill_switch_activated" for row in rows)
    assert any(row["stage"] == "symbol_kill_switch_blocked" for row in rows)


@pytest.mark.asyncio
async def test_composable_strategy_blocks_repeat_entry_when_active_exec_lot_exists(tmp_path):
    submit_calls = []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.decision_id, dict(envelope.extras)))
            return {"ok": True, "broker_order_id": "701", "perm_id": 701}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_active_lot_001",
        strategy_run_id="run-active-lot",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_active_lot_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_active_lot_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_active_lot_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_active_lot_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "require_exec_symbol_snapshot": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    feature_payload = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 101.0,
        "last_price": 500.0,
        "session": "RTH",
        "vwap": 499.0,
        "planned_qty": 1,
        "trigger_id": "t-active-lot",
        "bars_1m": [
            HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
            for i in range(1, 16)
        ],
    }
    await strategy.on_features("QQQ", dict(feature_payload))
    await strategy.on_features("QQQ", dict(feature_payload, ts_epoch_s=105.0, now_epoch_s=106.0))

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_active_lot_001", domain="strategy")

    assert len(submit_calls) == 1
    assert any(row["stage"] == "entry_blocked_active_binding_lot" for row in rows)


@pytest.mark.asyncio
async def test_composable_strategy_blocks_opposite_leg_when_binding_has_active_lot(tmp_path):
    submit_calls = []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.symbol, envelope.decision_id, dict(envelope.extras)))
            return {"ok": True, "broker_order_id": str(800 + len(submit_calls)), "perm_id": 800 + len(submit_calls)}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_binding_conflict_001",
        strategy_run_id="run-binding-conflict",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_binding_conflict_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_binding_conflict_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_binding_conflict_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_binding_conflict_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "v3m": 0.01,
            "trigger_id": "t-binding-long",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 105.0,
            "now_epoch_s": 106.0,
            "last_price": 499.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-binding-short",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=120 - i, high=121 - i, low=119 - i, close=120 - i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_binding_conflict_001", domain="strategy")

    assert len(submit_calls) == 1
    assert submit_calls[0][1] == "TQQQ"
    conflict_rows = [row for row in rows if row["stage"] == "entry_blocked_active_binding_lot"]
    assert conflict_rows
    assert conflict_rows[-1]["reason"] == "binding_conflict_active_lot"


@pytest.mark.asyncio
async def test_composable_strategy_quarantines_lot_when_submit_fails(tmp_path):
    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            raise RuntimeError("submit_failed")

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_submit_fail_001",
        strategy_run_id="run-submit-fail",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_submit_fail_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_submit_fail_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_submit_fail_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_submit_fail_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "require_exec_symbol_snapshot": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-submit-fail",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    lots = strategy.execution.ledger.list_lots(strategy_id="kernel_test_submit_fail_001", symbol="TQQQ", include_closed=True)
    assert len(lots) == 1
    assert lots[0].state == "quarantined"


@pytest.mark.asyncio
async def test_composable_strategy_handles_order_and_position_updates(tmp_path):
    submit_calls = []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append(dict(envelope.extras))
            return {"ok": True, "broker_order_id": "301", "perm_id": 301}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_updates_001",
        strategy_run_id="run-updates",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_updates_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_updates_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_updates_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_updates_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-updates",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    order_ref = str(submit_calls[0]["order_ref"])
    lot_id = str(submit_calls[0]["position_lot_id"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="301",
            strategy_id="kernel_test_updates_001",
            symbol="TQQQ",
            state="SUBMITTED",
            order_ref=order_ref,
            broker_order_id="301",
            ib_perm_id=301,
        )
    )
    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="kernel_test_updates_001",
            symbol="TQQQ",
            pos_qty=1.0,
            avg_px=500.0,
        )
    )
    await strategy.on_order_update(
        OrderUpdate(
            order_id="301",
            strategy_id="kernel_test_updates_001",
            symbol="TQQQ",
            state="CANCELLED",
            order_ref=order_ref,
            broker_order_id="301",
            ib_perm_id=301,
        )
    )
    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="kernel_test_updates_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=None,
        )
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_updates_001", domain="strategy")
    lot = strategy.execution.ledger.get_lot(lot_id)

    assert lot is not None and lot.state == "closed"
    assert lot.filled_qty >= 1.0
    assert any(row["stage"] == "order_update_received" for row in rows)
    assert any(row["stage"] == "position_update_received" for row in rows)


@pytest.mark.asyncio
async def test_composable_strategy_bridges_fill_and_db_persist_to_lot(tmp_path):
    submit_calls = []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append(dict(envelope.extras))
            return {"ok": True, "broker_order_id": "401", "perm_id": 401}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_fill_bridge_001",
        strategy_run_id="run-fill-bridge",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_fill_bridge_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_fill_bridge_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_fill_bridge_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_fill_bridge_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-fill-bridge",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    order_ref = str(submit_calls[0]["order_ref"])
    lot_id = str(submit_calls[0]["position_lot_id"])
    await strategy.on_fill_received(
        KernelFillReport(
            symbol="TQQQ",
            order_ref="broker-short-ref:TQQQ:ENT",
            broker_order_id="401",
            perm_id=401,
            qty=1.0,
            price=500.0,
            exec_id="exec-401",
        ),
        trace_id="trace-401",
        decision_id="dec-401",
    )
    await strategy.on_db_persist_result(
        order_ref=order_ref,
        symbol="TQQQ",
        success=True,
        trace_id="trace-401",
        decision_id="dec-401",
        broker_order_id="401",
        perm_id=401,
        payload={"db_order_id": "db-1"},
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    exec_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_fill_bridge_001", domain="exec")
    lot = strategy.execution.ledger.get_lot(lot_id)
    pending_count, pending_refs = strategy.order_lifecycle_module.pending_non_terminal_orders(symbols=["TQQQ"])

    assert lot is not None
    assert lot.state == "live"
    assert float(lot.live_qty) == 1.0
    assert pending_count == 0
    assert pending_refs == ()
    assert lot.extras["db_persist_success"] is True
    assert lot.extras["last_fill_feedback"]["position_lot_id"] == lot_id
    assert any(row["stage"] == "fill_received" for row in exec_rows)
    assert any(row["stage"] == "db_persisted" for row in exec_rows)


@pytest.mark.asyncio
async def test_composable_strategy_submits_live_exit_when_stop_triggers(tmp_path):
    submit_calls = []

    class _Engine:
        market_data_service = SimpleNamespace(ib_adapter=None)

        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((envelope.symbol, envelope.target_qty, dict(envelope.extras), envelope.decision_id))
            return {"ok": True, "broker_order_id": str(500 + len(submit_calls)), "perm_id": 500 + len(submit_calls)}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_live_exit_001",
        strategy_run_id="run-live-exit",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_live_exit_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_live_exit_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_live_exit_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_live_exit_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "require_exec_symbol_snapshot": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-live-exit",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    entry_order_ref = str(submit_calls[0][2]["order_ref"])
    lot_id = str(submit_calls[0][2]["position_lot_id"])
    await strategy.on_fill_received(
        KernelFillReport(
            symbol="TQQQ",
            order_ref=entry_order_ref,
            broker_order_id="501",
            perm_id=501,
            qty=1.0,
            price=500.0,
            exec_id="exec-live-exit-entry",
        )
    )
    await strategy.on_features(
        "TQQQ",
        {
            "ts_epoch_s": 110.0,
            "now_epoch_s": 111.0,
            "last_price": 497.0,
            "session": "RTH",
            "vwap": 499.0,
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )
    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    strategy_rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_live_exit_001", domain="strategy")
    lot = strategy.execution.ledger.get_lot(lot_id)

    assert len(submit_calls) == 2
    assert submit_calls[1][0] == "TQQQ"
    assert submit_calls[1][1] == 0.0
    assert submit_calls[1][2]["position_lot_id"] == lot_id
    assert submit_calls[1][2]["exit_order_role"] == "STOP"
    assert lot is not None and lot.flatten_order_refs
    assert any(row["stage"] == "exit_plan_triggered" for row in strategy_rows)
    assert any(row["stage"] == "exit_intent_submit_succeeded" for row in strategy_rows)


@pytest.mark.asyncio
async def test_composable_strategy_broker_truth_mismatch_freezes_exec_symbol(tmp_path):
    class _Broker:
        async def open_orders(self, force_refresh=True):
            return [
                SimpleNamespace(symbol="TQQQ", order_ref="stkv1:foreign_strategy:TQQQ:ENTRY:TRG:x:LOT:y", broker_order_id="999", perm_id=999)
            ]

    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        market_data_service = SimpleNamespace(ib_adapter=None)

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_truth_sync_001",
        strategy_run_id="run-truth-sync",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_truth_sync_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_truth_sync_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
    )
    ctx.engine.broker_adapter = _Broker()
    params = SimpleNamespace(
        strategy_id="kernel_test_truth_sync_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-truth-sync",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_truth_sync_001", domain="strategy")

    assert any(row["stage"] == "broker_truth_sync_mismatch" for row in rows)
    assert any(row["stage"] == "symbol_kill_switch_activated" and row["symbol"] == "TQQQ" for row in rows)
    assert any(row["stage"] == "symbol_kill_switch_released" and row["symbol"] == "TQQQ" for row in rows)


@pytest.mark.asyncio
async def test_composable_strategy_requests_eod_flatten_for_live_lot(tmp_path):
    submit_calls = []

    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        market_data_service = SimpleNamespace(ib_adapter=None)

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append((strategy_id, envelope.symbol, dict(envelope.extras)))
            return {"ok": True, "broker_order_id": "301", "perm_id": 301}

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_test_eod_flatten_001",
        strategy_run_id="run-eod",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_test_eod_flatten_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_test_eod_flatten_001", symbols),
        history=lambda symbol, resolution, limit: _Engine().history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: _Engine().submit_intent("kernel_test_eod_flatten_001", envelope),
    )
    params = SimpleNamespace(
        strategy_id="kernel_test_eod_flatten_001",
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "audit_root_dir": str(tmp_path),
            "flatten_before_close_et": "15:55",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-eod-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d1",
            opened_by_trigger_id="t1",
            entry_order_ref="oref-eod-1",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=100.0,
            state="live",
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": datetime(2026, 1, 5, 15, 56, tzinfo=ET).timestamp(),
            "now_epoch_s": datetime(2026, 1, 5, 15, 56, tzinfo=ET).timestamp(),
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 1,
            "trigger_id": "t-eod",
            "bars_1m": [
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ],
        },
    )

    if strategy.audit_writer is not None:
        strategy.audit_writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, strategy_id="kernel_test_eod_flatten_001", domain="strategy")

    assert len(submit_calls) == 1
    assert submit_calls[0][1] == "TQQQ"
    assert submit_calls[0][2]["exit_order_role"] == "FLATTEN"
    assert submit_calls[0][2]["reason"] == "eod_flatten"
    assert any(row["stage"] == "eod_flatten_requested" for row in rows)
