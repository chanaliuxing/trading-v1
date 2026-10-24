from types import SimpleNamespace

import pytest

from strategies.stocks_kernel.runtime.audit import LocalAuditWriter, aggregate_audit_chain
from strategies.stocks_kernel.runtime.certification import run_strategy_certification
from strategies.stocks_kernel.runtime.explain import explain_symbol_state
from strategies.stocks_kernel.runtime.matrix import MatrixScenario, run_regression_matrix
from strategies.stocks_kernel.runtime.readiness import generate_production_readiness_report
from strategies.stocks_kernel.runtime.replay import ReplayFrame
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import HistoryBar, KernelExecutionEvent, PositionLot


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _Engine:
    async def subscribe_features(self, strategy_id, symbols, bar_sec):
        return None

    async def subscribe_tick_data(self, strategy_id, symbols):
        return {symbol: True for symbol in symbols}


@pytest.mark.asyncio
async def test_explain_and_certification_expose_runtime_shape():
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_explain_001",
        strategy_run_id="run-1",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_explain_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_explain_001", symbols),
    )
    params = SimpleNamespace(
        strategy_id="kernel_explain_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    frames = [
        ReplayFrame(
            symbol="QQQ",
            features={
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
    ]

    report = await run_strategy_certification(strategy=strategy, frames=frames, required_symbols=("QQQ",))
    explained = explain_symbol_state(strategy=strategy, symbol="QQQ")

    assert report.passed is True
    assert report.frames == 1
    assert explained["symbol"] == "QQQ"
    assert explained["signal_action"] == "enter"
    assert "ENTRY" in explained["action_candidates"]
    assert explained["data_context_summary"]["signal"]["symbols"] == ("QQQ",)


@pytest.mark.asyncio
async def test_strategy_certification_fails_when_required_stage_missing():
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_cert_stage_missing_001",
        strategy_run_id="run-stage-missing-1",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_cert_stage_missing_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_cert_stage_missing_001", symbols),
    )
    params = SimpleNamespace(
        strategy_id="kernel_cert_stage_missing_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    frames = [
        ReplayFrame(
            symbol="QQQ",
            features={
                "ts_epoch_s": 100.0,
                "now_epoch_s": 101.0,
                "last_price": 500.0,
                "session": "RTH",
                "vwap": 499.0,
                "planned_qty": 2,
                "trigger_id": "t-stage-missing",
                "bars_1m": [
                    HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                    for i in range(1, 16)
                ],
            },
        )
    ]

    report = await run_strategy_certification(
        strategy=strategy,
        frames=frames,
        required_symbols=("QQQ",),
        required_stages=("non_existing_stage",),
    )

    assert report.passed is False
    assert "stage_missing:non_existing_stage" in report.findings


@pytest.mark.asyncio
async def test_regression_matrix_runner_executes_multiple_scenarios():
    async def _build_strategy(strategy_id: str, trigger_id: str):
        ctx = SimpleNamespace(
            log=_Logger(),
            metrics=SimpleNamespace(),
            engine=_Engine(),
            strategy_id=strategy_id,
            strategy_run_id=f"{strategy_id}-run",
            subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features(strategy_id, symbols, bar_sec),
            subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data(strategy_id, symbols),
        )
        params = SimpleNamespace(
            strategy_id=strategy_id,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "feature_set": ["vwap", "atr_eff", "v3m"],
                "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
                "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
            },
        )
        strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
        await strategy.initialize()
        strategy._test_trigger_id = trigger_id
        return strategy

    def _frame(trigger_id: str, price: float) -> ReplayFrame:
        return ReplayFrame(
            symbol="QQQ",
            features={
                "ts_epoch_s": 100.0,
                "now_epoch_s": 101.0,
                "last_price": price,
                "session": "RTH",
                "vwap": price - 1.0,
                "planned_qty": 2,
                "trigger_id": trigger_id,
                "bars_1m": [
                    HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                    for i in range(1, 16)
                ],
            },
        )

    report = await run_regression_matrix(
        scenarios=(
            MatrixScenario(
                name="trend_long",
                strategy_factory=lambda: _build_strategy("kernel_matrix_001", "trend_long"),
                frames=(_frame("trend_long", 500.0),),
                required_symbols=("QQQ",),
                required_stages=("signal_evaluated",),
            ),
            MatrixScenario(
                name="mean_revert_long",
                strategy_factory=lambda: _build_strategy("kernel_matrix_002", "mean_revert_long"),
                frames=(_frame("mean_revert_long", 300.0),),
                required_symbols=("QQQ",),
                required_stages=("signal_evaluated",),
            ),
        )
    )

    assert report.passed is True
    assert report.scenario_count == 2
    assert report.passed_count == 2
    assert {result.name for result in report.results} == {"trend_long", "mean_revert_long"}
    assert report.results[0].payload["explain"]["QQQ"]["symbol"] == "QQQ"


def test_aggregate_audit_chain_summarizes_multi_strategy_rows(tmp_path):
    writer = LocalAuditWriter(root_dir=tmp_path)
    writer.write_event(
        domain="strategy",
        event=KernelExecutionEvent(stage="entry_blocked_ownership", strategy_id="alpha", symbol="TQQQ"),
    )
    writer.write_event(
        domain="strategy",
        event=KernelExecutionEvent(stage="symbol_kill_switch_activated", strategy_id="alpha", symbol="TQQQ"),
    )
    writer.write_event(
        domain="strategy",
        event=KernelExecutionEvent(stage="restart_protection_rebuilt", strategy_id="beta", symbol="SOXL"),
    )

    summary = aggregate_audit_chain(root_dir=tmp_path, strategy_ids=("alpha", "beta"), domain="strategy")

    assert summary["rows"] == 3
    assert summary["by_strategy"] == {"alpha": 2, "beta": 1}
    assert summary["by_symbol"] == {"SOXL": 1, "TQQQ": 2}
    assert summary["blocked_events"] == 1
    assert summary["kill_switch_events"] == 1
    assert summary["protection_events"] == 1


@pytest.mark.asyncio
async def test_generate_production_readiness_report_blocks_on_kill_switch_and_unsafe_lot():
    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=_Engine(),
        strategy_id="kernel_ready_001",
        strategy_run_id="run-ready-1",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_ready_001", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_ready_001", symbols),
    )
    params = SimpleNamespace(
        strategy_id="kernel_ready_001",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "startup_preflight_enabled": False,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy._symbol_kill_switch["TQQQ"] = "broker_truth_mismatch"
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-ready-unsafe-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="oref",
            live_qty=1.0,
            filled_qty=1.0,
            avg_entry_price=100.0,
            state="live",
            extras={"protection_state": {"protection_lifecycle": "flatten_only", "freeze_required": True}},
        )
    )

    report = await generate_production_readiness_report(strategy=strategy)

    assert report.ready is False
    assert report.reason == "not_ready"
    assert {check.check_id for check in report.blocking_checks} >= {"symbol_kill_switches", "protection_invariants"}
    assert any(check.check_id == "startup_preflight" and check.status == "warn" for check in report.warning_checks)


@pytest.mark.asyncio
async def test_generate_production_readiness_report_passes_clean_runtime():
    class _Broker:
        async def open_orders(self, force_refresh=True):
            return []

        async def position_qty(self, symbol):
            return 0.0

    ctx = SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(broker_adapter=_Broker()),
        strategy_id="kernel_ready_002",
        strategy_run_id="run-ready-2",
        subscribe_features=lambda symbols, bar_sec=5: _Engine().subscribe_features("kernel_ready_002", symbols, bar_sec),
        subscribe_tick_data=lambda symbols: _Engine().subscribe_tick_data("kernel_ready_002", symbols),
    )
    params = SimpleNamespace(
        strategy_id="kernel_ready_002",
        ttl_ms=2000,
        extras={
            "dry_run": True,
            "startup_preflight_enabled": True,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    report = await generate_production_readiness_report(strategy=strategy)

    assert report.ready is True
    assert report.reason == "ready"
    assert report.blocking_checks == ()
