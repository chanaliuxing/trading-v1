#!/usr/bin/env python3
"""
End-to-end smoke script for multi-strategy same-symbol isolation.

Coverage:
1) ExecutionService close-only/flatten guard:
   - Strategy auto flatten must NOT use broker net position.
   - Manual emergency flatten may use broker net position.
   - Qty clamp and side semantics remain correct under isolation.
2) Strategy ownership isolation:
   - qqq_intraday/index_super_win must not claim broker net qty when ctx qty is flat.
   - Missing/failed ctx position must fail-safe to flat (0).
3) RuntimeReconciler safety:
   - position_sync_enabled is force-disabled to avoid strategy-position contamination.

Run:
  ./venv/bin/python tool/smoke/test_multi_strategy_isolation_chain.py
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

from libs.strategy import StrategyContext, StrategyParams
from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.reconciliation.runtime_reconciler import RuntimeReconciler
from strategies.index_super_win_strategy import IndexSuperWinStrategy
from strategies.qqq_intraday_strategy import QQQIntradayStrategy


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str = ""


class FakePg:
    def __init__(self, pos_map: dict[tuple[str, str], float]):
        self.pos_map = dict(pos_map)
        self.execute = AsyncMock(return_value=None)
        self.fetchall = AsyncMock(return_value=[])

    async def fetchone(self, sql: str, *args):
        if "FROM core.core_position" in sql:
            strategy_id, symbol = args[0], str(args[1]).upper()
            if (strategy_id, symbol) in self.pos_map:
                return {"pos_qty": self.pos_map[(strategy_id, symbol)]}
            return None
        if "SELECT order_id::text AS order_id" in sql:
            return {"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None}
        return None


def make_result(*, strategy_id: str, symbol: str, side: str, qty: float, extras: dict) -> PipelineResult:
    return PipelineResult(
        status="APPROVED",
        decision_id=f"dec-{strategy_id}-{side}-{qty}",
        strategy_id=strategy_id,
        symbol=symbol,
        plan=ExecPlan(
            order_type="MKT",
            side=side,
            qty=float(qty),
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={
            "trigger_id": f"trigger:{strategy_id}",
            "strategy_run_id": f"run:{strategy_id}",
            "trace_id": f"trace:{strategy_id}",
            "envelope_extras": dict(extras),
        },
    )


def make_strategy_ctx(strategy_id: str) -> StrategyContext:
    class _DummyLogger:
        def info(self, *args, **kwargs): ...
        def warning(self, *args, **kwargs): ...
        def error(self, *args, **kwargs): ...
        def debug(self, *args, **kwargs): ...

    class _DummyEngine:
        pass

    return StrategyContext(
        engine=_DummyEngine(),
        strategy_id=strategy_id,
        logger=_DummyLogger(),
        metrics=lambda *a, **k: None,
    )


async def case_execution_isolation_non_manual_blocked() -> CaseResult:
    pg = FakePg(pos_map={("qqq_intraday_001", "TQQQ"): 0.0})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)

    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)  # account net position exists
    broker.open_orders = AsyncMock(return_value=[])
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None})
    svc._place_order = AsyncMock(return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=1, order_ref="ref-1"))

    result = make_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        qty=137.0,
        extras={"flatten_action": True, "close_only": True},
    )
    out = await svc.execute(result)
    if out.success:
        return CaseResult("execution_non_manual_flatten_blocked", False, "expected rejection, got success")
    if not str(out.reason).startswith("close_only_no_long_position"):
        return CaseResult("execution_non_manual_flatten_blocked", False, f"unexpected reason={out.reason}")
    if svc._place_order.await_count != 0:
        return CaseResult("execution_non_manual_flatten_blocked", False, "place_order should not be called")
    return CaseResult("execution_non_manual_flatten_blocked", True)


async def case_execution_non_manual_flatten_allowed_with_strategy_qty() -> CaseResult:
    pg = FakePg(pos_map={("qqq_intraday_001", "TQQQ"): 50.0})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)

    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    broker.open_orders = AsyncMock(return_value=[])
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None})
    svc._place_order = AsyncMock(return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=1, order_ref="ref-1"))

    result = make_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        qty=50.0,
        extras={"flatten_action": True, "close_only": True},
    )
    out = await svc.execute(result)
    if not out.success:
        return CaseResult("execution_non_manual_flatten_allowed_with_strategy_qty", False, f"unexpected rejection={out.reason}")
    if svc._place_order.await_count != 1:
        return CaseResult("execution_non_manual_flatten_allowed_with_strategy_qty", False, "place_order should be called")
    return CaseResult("execution_non_manual_flatten_allowed_with_strategy_qty", True)


async def case_execution_close_only_clamp_qty() -> CaseResult:
    pg = FakePg(pos_map={("qqq_intraday_001", "TQQQ"): 30.0})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)

    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    broker.open_orders = AsyncMock(return_value=[])
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None})
    captured = {}

    async def _fake_place_order(*, strategy_id, symbol, plan, envelope_extras):
        captured["qty"] = float(plan.qty)
        return SimpleNamespace(order_id=101, broker_order_id="101", perm_id=1, order_ref="ref-1")

    svc._place_order = AsyncMock(side_effect=_fake_place_order)

    result = make_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        qty=100.0,
        extras={"flatten_action": True, "close_only": True},
    )
    out = await svc.execute(result)
    if not out.success:
        return CaseResult("execution_close_only_clamp_qty", False, f"unexpected rejection={out.reason}")
    if abs(float(captured.get("qty", -1.0)) - 30.0) > 1e-9:
        return CaseResult("execution_close_only_clamp_qty", False, f"expected qty=30 got {captured.get('qty')}")
    return CaseResult("execution_close_only_clamp_qty", True)


async def case_execution_option_close_only_uses_hint() -> CaseResult:
    # DB row missing (lag), but strategy provides position_qty_hint for option close-only.
    # NOTE: For options, explicit DB zero is treated as authoritative flat and should reject close-only.
    pg = FakePg(pos_map={})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)

    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=0.0)
    broker.open_orders = AsyncMock(return_value=[])
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None})
    svc._place_order = AsyncMock(return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=1, order_ref="ref-1"))

    result = make_result(
        strategy_id="qqq_0dte_opt_live_001",
        symbol="OPT:QQQ:20260305:500:C",
        side="SELL",
        qty=3.0,
        extras={"instrument_type": "OPTION", "close_only": True, "position_qty_hint": 3.0},
    )
    out = await svc.execute(result)
    if not out.success:
        return CaseResult("execution_option_close_only_uses_hint", False, f"unexpected rejection={out.reason}")
    if svc._place_order.await_count != 1:
        return CaseResult("execution_option_close_only_uses_hint", False, "place_order should be called")
    return CaseResult("execution_option_close_only_uses_hint", True)


async def case_execution_protective_stop_blocked_when_no_strategy_qty() -> CaseResult:
    pg = FakePg(pos_map={("qqq_intraday_001", "TQQQ"): 0.0})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)

    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    broker.open_orders = AsyncMock(return_value=[])
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None})
    svc._place_order = AsyncMock(return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=1, order_ref="ref-1"))

    result = make_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        qty=137.0,
        extras={"protective_stop": True, "close_only": True},
    )
    out = await svc.execute(result)
    if out.success:
        return CaseResult("execution_protective_stop_blocked_when_no_strategy_qty", False, "expected rejection")
    if not str(out.reason).startswith("close_only_no_long_position"):
        return CaseResult("execution_protective_stop_blocked_when_no_strategy_qty", False, f"unexpected reason={out.reason}")
    return CaseResult("execution_protective_stop_blocked_when_no_strategy_qty", True)


async def case_execution_isolation_manual_allowed() -> CaseResult:
    pg = FakePg(pos_map={("qqq_intraday_001", "TQQQ"): 0.0})
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=None)

    broker = MagicMock()
    broker.position_qty = AsyncMock(return_value=137.0)
    broker.open_orders = AsyncMock(return_value=[])
    broker.cancel = AsyncMock(return_value=True)
    kernel = MagicMock()
    kernel.broker = broker

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
    )
    svc._persist_order_new = AsyncMock(return_value=None)
    svc._update_order_submitted = AsyncMock(return_value=None)
    svc._get_order_identity = AsyncMock(return_value={"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None})
    svc._place_order = AsyncMock(return_value=SimpleNamespace(order_id=101, broker_order_id="101", perm_id=1, order_ref="manual-ref-1"))

    result = make_result(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        qty=137.0,
        extras={
            "manual_intervention": True,
            "flatten_action": True,
            "close_only": True,
            "manual_source": "smoke_script",
        },
    )
    out = await svc.execute(result)
    if not out.success:
        return CaseResult("execution_manual_flatten_allowed", False, f"unexpected rejection={out.reason}")
    if svc._place_order.await_count != 1:
        return CaseResult("execution_manual_flatten_allowed", False, "place_order should be called exactly once")
    return CaseResult("execution_manual_flatten_allowed", True)


async def case_strategy_qty_isolation() -> CaseResult:
    params = StrategyParams()
    params.extras = {
        "enable_observability_file": False,
        "fail_fast_enabled": False,
        "submit_exchange_stop_intent": True,
    }

    qqq_ctx = make_strategy_ctx("qqq_intraday_001")
    qqq_ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=0.0))
    qqq = QQQIntradayStrategy(qqq_ctx, params)
    qqq._broker_position_qty = AsyncMock(return_value=137.0)
    qqq_qty = await qqq._current_exec_qty_for_symbol("TQQQ")

    idx_ctx = make_strategy_ctx("index_super_win_001")
    idx_ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=0.0))
    idx = IndexSuperWinStrategy(idx_ctx, params)
    idx._broker_position_qty = AsyncMock(return_value=137.0)
    idx_qty = await idx._current_exec_qty_for_symbol("TQQQ")

    if abs(float(qqq_qty)) > 1e-9 or abs(float(idx_qty)) > 1e-9:
        return CaseResult(
            "strategy_qty_isolation",
            False,
            f"expected both 0.0, got qqq={qqq_qty}, index={idx_qty}",
        )
    return CaseResult("strategy_qty_isolation", True)


async def case_strategy_ctx_missing_fail_safe_flat() -> CaseResult:
    params = StrategyParams()
    params.extras = {"enable_observability_file": False}

    qqq_ctx = make_strategy_ctx("qqq_intraday_001")
    qqq_ctx.position = AsyncMock(return_value=None)
    qqq = QQQIntradayStrategy(qqq_ctx, params)
    qqq._broker_position_qty = AsyncMock(return_value=137.0)
    qqq_qty = await qqq._current_exec_qty_for_symbol("TQQQ")

    idx_ctx = make_strategy_ctx("index_super_win_001")
    idx_ctx.position = AsyncMock(return_value=None)
    idx = IndexSuperWinStrategy(idx_ctx, params)
    idx._broker_position_qty = AsyncMock(return_value=137.0)
    idx_qty = await idx._current_exec_qty_for_symbol("TQQQ")

    # Current production hardening:
    # - qqq_intraday may fall back to broker qty when ctx/DB is missing (restart window).
    # - index_super_win remains fail-safe flat to avoid claiming broker net inventory.
    if abs(float(qqq_qty) - 137.0) > 1e-9 or abs(float(idx_qty)) > 1e-9:
        return CaseResult("strategy_ctx_missing_fail_safe_flat", False, f"qqq={qqq_qty}, index={idx_qty}")
    return CaseResult("strategy_ctx_missing_fail_safe_flat", True)


async def case_strategy_ctx_exception_fail_safe_flat() -> CaseResult:
    params = StrategyParams()
    params.extras = {"enable_observability_file": False}

    qqq_ctx = make_strategy_ctx("qqq_intraday_001")
    qqq_ctx.position = AsyncMock(side_effect=RuntimeError("ctx down"))
    qqq = QQQIntradayStrategy(qqq_ctx, params)
    qqq._broker_position_qty = AsyncMock(return_value=137.0)
    qqq_qty = await qqq._current_exec_qty_for_symbol("TQQQ")

    idx_ctx = make_strategy_ctx("index_super_win_001")
    idx_ctx.position = AsyncMock(side_effect=RuntimeError("ctx down"))
    idx = IndexSuperWinStrategy(idx_ctx, params)
    idx._broker_position_qty = AsyncMock(return_value=137.0)
    idx_qty = await idx._current_exec_qty_for_symbol("TQQQ")

    if abs(float(qqq_qty)) > 1e-9 or abs(float(idx_qty)) > 1e-9:
        return CaseResult("strategy_ctx_exception_fail_safe_flat", False, f"qqq={qqq_qty}, index={idx_qty}")
    return CaseResult("strategy_ctx_exception_fail_safe_flat", True)


async def case_runtime_reconciler_position_sync_forced_off() -> CaseResult:
    pg = MagicMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, position_sync_enabled=True)
    if reconciler.position_sync_enabled is not False:
        return CaseResult("runtime_reconciler_position_sync_forced_off", False, "position_sync_enabled should be False")
    return CaseResult("runtime_reconciler_position_sync_forced_off", True)


async def main() -> int:
    cases = [
        await case_execution_isolation_non_manual_blocked(),
        await case_execution_non_manual_flatten_allowed_with_strategy_qty(),
        await case_execution_close_only_clamp_qty(),
        await case_execution_option_close_only_uses_hint(),
        await case_execution_protective_stop_blocked_when_no_strategy_qty(),
        await case_execution_isolation_manual_allowed(),
        await case_strategy_qty_isolation(),
        await case_strategy_ctx_missing_fail_safe_flat(),
        await case_strategy_ctx_exception_fail_safe_flat(),
        await case_runtime_reconciler_position_sync_forced_off(),
    ]
    print("=== Multi-Strategy Isolation Chain Smoke ===")
    failed = 0
    for c in cases:
        status = "PASS" if c.ok else "FAIL"
        msg = f"{status} | {c.name}"
        if c.detail:
            msg += f" | {c.detail}"
        print(msg)
        if not c.ok:
            failed += 1
    print(f"SUMMARY | total={len(cases)} failed={failed} passed={len(cases)-failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
