from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "strategies" in sys.modules:
    del sys.modules["strategies"]

from strategies.stocks_kernel.runtime.readiness import generate_production_readiness_report
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import HistoryBar, KernelFillReport, PositionLot

ET = ZoneInfo("America/New_York")


class _Logger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def _bars() -> list[HistoryBar]:
    return [
        HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
        for i in range(1, 16)
    ]


def _ctx(strategy_id: str, submit_calls: list[dict], broker_adapter=None) -> SimpleNamespace:
    class _Engine:
        async def subscribe_features(self, strategy_id, symbols, bar_sec):
            return None

        async def subscribe_tick_data(self, strategy_id, symbols):
            return {symbol: True for symbol in symbols}

        async def history_features(self, symbol, resolution, limit, fields=None):
            return []

        market_data_service = SimpleNamespace(ib_adapter=None)

        async def submit_intent(self, strategy_id, envelope):
            submit_calls.append(
                {
                    "strategy_id": strategy_id,
                    "symbol": envelope.symbol,
                    "target_qty": float(envelope.target_qty or 0.0),
                    "extras": dict(envelope.extras),
                }
            )
            return {"ok": True, "broker_order_id": "501", "perm_id": 501}

    engine = _Engine()
    engine.broker_adapter = broker_adapter
    return SimpleNamespace(
        log=_Logger(),
        metrics=SimpleNamespace(),
        engine=engine,
        strategy_id=strategy_id,
        strategy_run_id=f"{strategy_id}-run",
        subscribe_features=lambda symbols, bar_sec=5: engine.subscribe_features(strategy_id, symbols, bar_sec),
        subscribe_tick_data=lambda symbols: engine.subscribe_tick_data(strategy_id, symbols),
        history=lambda symbol, resolution, limit: engine.history_features(symbol, resolution, limit),
        submit_intent=lambda envelope: engine.submit_intent(strategy_id, envelope),
    )


def _params(strategy_id: str, **extras) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id=strategy_id,
        ttl_ms=2000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
            **extras,
        },
    )


@pytest.mark.asyncio
async def test_stocks_kernel_integration_same_account_entry_block_then_eod_flatten():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    alpha = ComposableStocksKernelStrategy(ctx=_ctx("stocks_kernel_int_alpha_001", alpha_calls), params=_params("stocks_kernel_int_alpha_001", flatten_before_close_et="15:55"))
    beta = ComposableStocksKernelStrategy(ctx=_ctx("stocks_kernel_int_beta_001", beta_calls), params=_params("stocks_kernel_int_beta_001", flatten_before_close_et="15:55"))
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    base_features = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 101.0,
        "last_price": 500.0,
        "session": "RTH",
        "vwap": 499.0,
        "planned_qty": 1,
        "trigger_id": "int-entry",
        "bars_1m": _bars(),
    }

    await alpha.on_features("QQQ", dict(base_features))
    await alpha.on_fill_received(
        KernelFillReport(
            symbol="TQQQ",
            order_ref=str(alpha_calls[0]["extras"]["order_ref"]),
            broker_order_id="b1",
            perm_id=1,
            qty=1.0,
            price=500.0,
            exec_id="fill-alpha-entry",
        )
    )
    await beta.on_features("QQQ", dict(base_features))
    await alpha.on_features(
        "QQQ",
        {
            **base_features,
            "ts_epoch_s": datetime(2026, 1, 5, 15, 56, tzinfo=ET).timestamp(),
            "now_epoch_s": datetime(2026, 1, 5, 15, 56, tzinfo=ET).timestamp(),
            "trigger_id": "int-eod",
        },
    )

    assert len(alpha_calls) == 2
    assert alpha_calls[0]["extras"]["order_ref"].startswith("stkv1:stocks_kernel_int_alpha_001:TQQQ:ENTRY")
    assert alpha_calls[1]["extras"]["exit_order_role"] == "FLATTEN"
    assert len(beta_calls) == 0


@pytest.mark.asyncio
async def test_stocks_kernel_integration_restart_recovery_and_readiness():
    submit_calls: list[dict] = []
    strategy = ComposableStocksKernelStrategy(
        ctx=_ctx("stocks_kernel_int_recovery_001", submit_calls),
        params=_params("stocks_kernel_int_recovery_001"),
    )
    await strategy.initialize()
    strategy.order_lifecycle_module = None
    strategy._remember_latest_snapshot(symbol="QQQ", features={"ts_epoch_s": 100.0, "last_price": 500.0, "session": "RTH"})
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-int-recovery-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d",
            opened_by_trigger_id=None,
            entry_order_ref="stkv1:int:TQQQ:ENTRY",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=100.0,
            state="live",
            extras={
                "protective_oco": {"sl_offset_abs": 1.0, "tp_offset_abs": 2.0},
                "protection_state": {
                    "position_lifecycle": "live",
                    "protection_lifecycle": "repair_pending",
                    "management_phase": "pre_tp1",
                    "freeze_required": True,
                },
            },
        )
    )

    before = await generate_production_readiness_report(strategy=strategy)
    ready = await strategy.recovery_module.ensure_runtime_ready_for_tick(symbol="QQQ")
    after = await generate_production_readiness_report(strategy=strategy)

    assert before.ready is False
    assert ready is True
    assert len(submit_calls) == 1
    assert submit_calls[0]["extras"]["exit_order_role"] == "STOP"
    assert after.ready is True


@pytest.mark.asyncio
async def test_stocks_kernel_integration_same_symbol_multi_strategy_lane_scope_allows_parallel_entries():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    shared_universe = [
        {
            "signal_symbol": "QQQ",
            "long_exec_symbol": "TQQQ",
            "short_exec_symbol": "SQQQ",
            "execution_lanes": [
                {
                    "lane_id": "long_lane",
                    "symbol": "TQQQ",
                    "side": "LONG",
                    "role": "execution",
                    "ownership_scope": "lane",
                    "flatten_authority": "lane",
                    "protection_authority": "lane",
                    "adopt_authority": "lane",
                },
                {
                    "lane_id": "short_lane",
                    "symbol": "SQQQ",
                    "side": "SHORT",
                    "role": "execution",
                    "ownership_scope": "lane",
                    "flatten_authority": "lane",
                    "protection_authority": "lane",
                    "adopt_authority": "lane",
                },
            ],
        }
    ]
    alpha = ComposableStocksKernelStrategy(
        ctx=_ctx("stocks_kernel_lane_alpha_001", alpha_calls),
        params=_params(
            "stocks_kernel_lane_alpha_001",
            default_ownership_scope="lane",
            universe=shared_universe,
        ),
    )
    beta = ComposableStocksKernelStrategy(
        ctx=_ctx("stocks_kernel_lane_beta_001", beta_calls),
        params=_params(
            "stocks_kernel_lane_beta_001",
            default_ownership_scope="lane",
            universe=shared_universe,
        ),
    )
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    base_features = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 101.0,
        "last_price": 500.0,
        "session": "RTH",
        "vwap": 499.0,
        "planned_qty": 1,
        "trigger_id": "lane-entry",
        "bars_1m": _bars(),
    }

    await alpha.on_features("QQQ", dict(base_features))
    await beta.on_features("QQQ", dict(base_features))

    assert len(alpha_calls) == 1
    assert len(beta_calls) == 1
    assert alpha_calls[0]["extras"]["lane_id"] == "long_lane"
    assert beta_calls[0]["extras"]["lane_id"] == "long_lane"
    shared_lots = alpha.execution.ledger.list_lots(symbol="TQQQ", include_closed=False)
    assert len(shared_lots) >= 2
    assert {
        str(getattr(lot, "strategy_id", "") or "").strip()
        for lot in shared_lots
    } >= {"stocks_kernel_lane_alpha_001", "stocks_kernel_lane_beta_001"}


@pytest.mark.asyncio
async def test_stocks_kernel_integration_protection_lifecycle_tp1_to_trailing_flatten_close():
    submit_calls: list[dict] = []
    strategy = ComposableStocksKernelStrategy(
        ctx=_ctx("stocks_kernel_lifecycle_001", submit_calls),
        params=_params(
            "stocks_kernel_lifecycle_001",
            universe=[{"signal_symbol": "TQQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
            enable_tp2_lifecycle=False,
            enable_breakeven_shield=True,
            enable_dynamic_stop_widen=True,
            policy_params={"max_trades_per_day": 1},
        ),
    )
    await strategy.initialize()

    base_features = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 101.0,
        "last_price": 100.0,
        "session": "RTH",
        "vwap": 99.5,
        "planned_qty": 2,
        "trigger_id": "int-lifecycle-entry",
        "bars_1m": _bars(),
    }

    # 1) Entry submit + entry fill.
    await strategy.on_features("TQQQ", dict(base_features))
    assert len(submit_calls) == 1
    entry_ref = str(submit_calls[0]["extras"]["order_ref"])
    await strategy.on_fill_received(
        KernelFillReport(
            symbol="TQQQ",
            order_ref=entry_ref,
            broker_order_id="lifecycle-entry-1",
            perm_id=11,
            qty=2.0,
            price=100.0,
            exec_id="lifecycle-entry-fill-1",
        )
    )
    lot = strategy.execution.ledger.list_lots(strategy_id=strategy.strategy_id, symbol="TQQQ", include_closed=False)[0]
    state = dict(lot.extras.get("protection_state") or {})
    tp1_trigger = float(state.get("target_price") or 0.0)
    assert tp1_trigger > 0.0

    # 2) Hit TP1, generate partial take-profit.
    await strategy.on_features(
        "TQQQ",
        {
            **base_features,
            "ts_epoch_s": 160.0,
            "now_epoch_s": 160.0,
            "last_price": tp1_trigger + 0.5,
            "trigger_id": "int-lifecycle-tp1",
        },
    )
    assert len(submit_calls) >= 2
    tp1_call = next(call for call in submit_calls if "exit_order_role" in dict(call.get("extras") or {}))
    assert tp1_call["extras"]["exit_order_role"] == "TAKE_PROFIT"
    tp1_ref = str(tp1_call["extras"]["order_ref"])
    tp1_qty = float(tp1_call["extras"]["exit_qty"] or 0.0)
    await strategy.on_fill_received(
        KernelFillReport(
            symbol="TQQQ",
            order_ref=tp1_ref,
            broker_order_id="lifecycle-tp1-1",
            perm_id=12,
            qty=tp1_qty,
            price=tp1_trigger + 0.6,
            exec_id="lifecycle-tp1-fill-1",
        )
    )
    lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    state = dict((lot.extras or {}).get("protection_state") or {})
    assert bool(state.get("tp1_done")) is True
    assert bool(state.get("trailing_armed")) is True

    # 3) Arm deterministic post-TP1 trailing state then break floor -> flatten.
    trail_floor = tp1_trigger + 0.4
    strategy.execution.ledger.patch_lot_extras(
        position_lot_id=lot.position_lot_id,
        updates={
            "protection_state": {
                **state,
                "trailing_armed": True,
                "management_phase": "post_tp1_trailing",
                "trail_anchor_price": tp1_trigger + 1.0,
                "trail_floor_price": trail_floor,
            }
        },
    )
    await strategy.on_features(
        "TQQQ",
        {
            **base_features,
            "ts_epoch_s": 220.0,
            "now_epoch_s": 220.0,
            "last_price": trail_floor - 0.2,
            "trigger_id": "int-lifecycle-trail-break",
        },
    )
    assert len(submit_calls) >= 3
    flatten_call = next(
        call
        for call in reversed(submit_calls)
        if str(dict(call.get("extras") or {}).get("exit_order_role") or "").upper() == "FLATTEN"
    )
    assert flatten_call["extras"]["exit_order_role"] == "FLATTEN"
    flatten_ref = str(flatten_call["extras"]["order_ref"])
    flatten_qty = float(flatten_call["extras"]["exit_qty"] or 0.0)
    await strategy.on_fill_received(
        KernelFillReport(
            symbol="TQQQ",
            order_ref=flatten_ref,
            broker_order_id="lifecycle-flat-1",
            perm_id=13,
            qty=flatten_qty,
            price=trail_floor - 0.25,
            exec_id="lifecycle-flat-fill-1",
        )
    )

    closed_lot = strategy.execution.ledger.get_lot(lot.position_lot_id)
    closed_state = dict((closed_lot.extras or {}).get("protection_state") or {})
    assert str(closed_lot.state or "") == "closed"
    assert str(closed_state.get("position_lifecycle") or "") == "closed"
    assert str(closed_state.get("protection_lifecycle") or "") == "closed"
