#!/usr/bin/env python3
"""
Offline smoke for index_super_win restart/replay hardening.

Coverage:
1) Restart with stale submitted runtime + owned open order must not resubmit duplicate entry.
2) Same-symbol foreign ownership must block takeover.
3) Startup consistency failure must disable entry before first signal.

Run:
  ./venv/bin/python tool/smoke/test_index_super_win_restart_replay.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.strategy import StrategyParams
from services.strategy_runtime_v2 import RuntimeLifecycle, RuntimeState, get_shared_ownership_service, get_shared_runtime_store
from strategies.index_super_win_strategy import IndexSuperWinStrategy


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str = ""


def make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.strategy_id = "index_super_win_001"
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib.positions.return_value = []
    ib.openTrades.return_value = []
    ib_adapter = MagicMock()
    ib_adapter.ib = ib
    ib_adapter.account = "DU1"
    market_data_service = MagicMock()
    market_data_service.ib_adapter = ib_adapter
    ctx.engine = MagicMock()
    ctx.engine.market_data_service = market_data_service
    ctx.subscribe_features = AsyncMock()
    ctx.set_cadence = AsyncMock()
    ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "reasons": []})
    ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    return ctx


async def case_restart_with_owned_open_order_blocks_duplicate_submit() -> CaseResult:
    scope = "smoke_index_super_win_restart_owned_order"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=int(1e6),
        )
    )
    ctx = make_ctx()
    ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=501,
                action="BUY",
                totalQuantity=5.0,
                orderType="LMT",
                orderRef="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    strategy = IndexSuperWinStrategy(
        ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "symbols": ["AAPL"],
                "dry_run": False,
                "runtime_scope": scope,
                "broker_reconcile_on_init": False,
                "enable_observability_file": False,
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "restart_duplicate_guard",
        },
    )
    preview = strategy._last_preview_by_symbol.get("AAPL") or {}
    if preview.get("status") != "RUNTIME_GUARD_BLOCKED":
        return CaseResult("restart_owned_open_order_duplicate_submit", False, f"unexpected preview={preview}")
    if ctx.submit_intent.await_count != 0:
        return CaseResult("restart_owned_open_order_duplicate_submit", False, "submit_intent should not be called")
    return CaseResult("restart_owned_open_order_duplicate_submit", True)


async def case_same_symbol_foreign_owner_blocks_takeover() -> CaseResult:
    scope = "smoke_index_super_win_foreign_owner"
    ownership = get_shared_ownership_service(scope)
    await ownership.claim(
        strategy_id="qqq_intraday_001",
        symbol="AAPL",
        qty=10.0,
        trigger_id="foreigntrg",
        order_namespace="qqqv1",
    )
    ctx = make_ctx()
    strategy = IndexSuperWinStrategy(
        ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "symbols": ["AAPL"],
                "dry_run": False,
                "runtime_scope": scope,
                "broker_reconcile_on_init": False,
                "enable_observability_file": False,
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "foreign_owner_conflict",
        },
    )
    diag = strategy.state.get("diag_snapshot") or {}
    conflict = (diag.get("conflict_report_by_symbol") or {}).get("AAPL") or {}
    if conflict.get("foreign_owner_strategy_id") != "qqq_intraday_001":
        return CaseResult("same_symbol_foreign_owner_blocks_takeover", False, f"unexpected conflict={conflict}")
    if ctx.submit_intent.await_count != 0:
        return CaseResult("same_symbol_foreign_owner_blocks_takeover", False, "submit_intent should not be called")
    return CaseResult("same_symbol_foreign_owner_blocks_takeover", True)


async def case_restart_foreign_open_order_does_not_trigger_cleanup() -> CaseResult:
    scope = "smoke_index_super_win_restart_foreign_open_order"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=1,
        )
    )
    ctx = make_ctx()
    ctx.engine.market_data_service.ib_adapter.ib.cancelOrder = MagicMock()
    ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=601,
                action="BUY",
                totalQuantity=10.0,
                orderType="LMT",
                orderRef="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:foreign",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    strategy = IndexSuperWinStrategy(
        ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "symbols": ["AAPL"],
                "dry_run": True,
                "runtime_scope": scope,
                "broker_reconcile_on_init": False,
                "enable_observability_file": False,
                "startup_stale_submitted_age_sec": 0.01,
            },
        ),
    )
    await strategy.initialize()
    state = await store.get_state("index_super_win_001", "AAPL")
    if state is None or state.lifecycle != RuntimeLifecycle.QUARANTINE:
        return CaseResult("restart_foreign_open_order_does_not_trigger_cleanup", False, f"unexpected state={state}")
    if ctx.engine.market_data_service.ib_adapter.ib.cancelOrder.call_count != 0:
        return CaseResult("restart_foreign_open_order_does_not_trigger_cleanup", False, "cancelOrder should not be called")
    return CaseResult("restart_foreign_open_order_does_not_trigger_cleanup", True)


async def case_startup_consistency_failure_disables_entry() -> CaseResult:
    ctx = make_ctx()
    ctx.engine.market_data_service.ib_adapter.ib.openTrades.side_effect = RuntimeError("open_trades_down")
    strategy = IndexSuperWinStrategy(
        ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "symbols": ["AAPL"],
                "dry_run": False,
                "broker_reconcile_on_init": False,
                "enable_observability_file": False,
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "startup_consistency_failure",
        },
    )
    preview = strategy._last_preview_by_symbol.get("AAPL") or {}
    if preview.get("status") != "STARTUP_NOT_READY":
        return CaseResult("startup_consistency_failure_disables_entry", False, f"unexpected preview={preview}")
    if ctx.submit_intent.await_count != 0:
        return CaseResult("startup_consistency_failure_disables_entry", False, "submit_intent should not be called")
    return CaseResult("startup_consistency_failure_disables_entry", True)


async def main() -> int:
    cases = [
        await case_restart_with_owned_open_order_blocks_duplicate_submit(),
        await case_same_symbol_foreign_owner_blocks_takeover(),
        await case_restart_foreign_open_order_does_not_trigger_cleanup(),
        await case_startup_consistency_failure_disables_entry(),
    ]
    passed = sum(1 for case in cases if case.ok)
    total = len(cases)
    for case in cases:
        status = "PASS" if case.ok else "FAIL"
        detail = f" :: {case.detail}" if case.detail else ""
        print(f"[{status}] {case.name}{detail}")
    print(f"\nSummary: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
