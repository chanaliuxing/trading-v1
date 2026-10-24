from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import ExitPlan, FeatureContext, HistoryBundle, MarketSnapshot, PositionLot

ET = ZoneInfo("America/New_York")


def _ctx(strategy_id: str):
    return SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id=strategy_id,
        strategy_run_id=f"run-{strategy_id}",
        subscribe_features=lambda symbols, bar_sec=5: None,
        subscribe_tick_data=lambda symbols: None,
        history=lambda symbol, resolution, limit: [],
    )


def _params(strategy_id: str):
    return SimpleNamespace(
        strategy_id=strategy_id,
        ttl_ms=2_000,
        extras={
            "dry_run": True,
            "startup_preflight_enabled": False,
            "order_namespace": "stkv1",
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "flatten_before_close_et": "15:55",
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )


def _open_live_lot(strategy: ComposableStocksKernelStrategy, *, lot_id: str, symbol: str, side: str, qty: float):
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id=lot_id,
            strategy_id=strategy.strategy_id,
            symbol=symbol,
            side=side,
            opened_by_decision_id=f"dec-{lot_id}",
            opened_by_trigger_id=f"trg-{lot_id}",
            entry_order_ref=f"oref-{lot_id}",
            live_qty=float(qty),
            filled_qty=float(qty),
            avg_entry_price=100.0,
            state="live",
        )
    )


@pytest.mark.asyncio
async def test_manage_live_exits_executes_exit_action_set_for_multiple_lots():
    strategy = ComposableStocksKernelStrategy(ctx=_ctx("kernel_test_action_set_live_exit_001"), params=_params("kernel_test_action_set_live_exit_001"))
    await strategy.initialize()
    instrument = strategy.config.instruments[0]
    _open_live_lot(strategy, lot_id="lot-live-1", symbol="TQQQ", side="LONG", qty=2.0)
    _open_live_lot(strategy, lot_id="lot-live-2", symbol="SQQQ", side="SHORT", qty=3.0)

    async_calls: list[str] = []

    def _select_exit_plan_for_lot(*, lot, snapshot, feature_context):
        _ = snapshot, feature_context
        return ExitPlan(
            symbol=lot.symbol,
            order_role="FLATTEN",
            qty=float(lot.live_qty),
            position_lot_id=lot.position_lot_id,
            payload={"reason": "test_action_set"},
        )

    async def _submit_exit_intent(*, exit_plan, snapshot, lot):
        _ = exit_plan, snapshot
        async_calls.append(str(lot.position_lot_id))

    strategy._select_exit_plan_for_lot = _select_exit_plan_for_lot
    strategy._submit_exit_intent = _submit_exit_intent

    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH", vwap=499.0)
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=snapshot,
        history=HistoryBundle(symbol="QQQ"),
        values={},
        readiness={},
    )
    await strategy._manage_live_exits(
        symbol="QQQ",
        instrument=instrument,
        snapshot=snapshot,
        feature_context=feature_context,
    )

    assert set(async_calls) == {"lot-live-1", "lot-live-2"}


@pytest.mark.asyncio
async def test_eod_flatten_executes_for_all_live_lots_in_same_tick():
    strategy = ComposableStocksKernelStrategy(ctx=_ctx("kernel_test_action_set_eod_001"), params=_params("kernel_test_action_set_eod_001"))
    await strategy.initialize()
    instrument = strategy.config.instruments[0]
    _open_live_lot(strategy, lot_id="lot-eod-1", symbol="TQQQ", side="LONG", qty=2.0)
    _open_live_lot(strategy, lot_id="lot-eod-2", symbol="SQQQ", side="SHORT", qty=1.0)

    flattened: list[str] = []

    async def _submit_exit_intent(*, exit_plan, snapshot, lot):
        _ = snapshot
        if str(exit_plan.order_role or "").upper() == "FLATTEN":
            flattened.append(str(lot.position_lot_id))

    strategy._submit_exit_intent = _submit_exit_intent
    snapshot = MarketSnapshot(
        symbol="QQQ",
        ts_epoch_s=datetime(2026, 1, 5, 15, 56, tzinfo=ET).timestamp(),
        last_price=500.0,
        session="RTH",
        vwap=499.0,
    )
    did_flatten = await strategy._maybe_force_eod_flatten(
        symbol="QQQ",
        instrument=instrument,
        snapshot=snapshot,
    )

    assert did_flatten is True
    assert set(flattened) == {"lot-eod-1", "lot-eod-2"}

