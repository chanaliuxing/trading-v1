#!/usr/bin/env python3
"""
Decision/Execution chain regression smoke.

Purpose:
1) Cover blind spots from previous smoke:
   - DecisionPipeline shadow path must finalize arbiter allocation.
   - Arbiter symbol-cap must read reservation ledger truth source.
2) Exercise a minimal full chain:
   - pipeline -> execution -> risk -> broker placement (mocked adapter).

Run:
  ./venv/bin/python tool/smoke/test_decision_chain_regression.py
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

from libs.strategy.base import DecisionEnvelope, Freshness
from services.decision.pipeline import DecisionPipelineService
from services.decision.providers import ReserveLedger
from services.exec.execution_service import ExecutionService


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str = ""


class _MS:
    async def get(self, symbol):
        return {
            "halted": False,
            "bid_px": 99.9,
            "ask_px": 100.1,
            "mid_px": 100.0,
            "last_px": 100.0,
            "tick_size": 0.01,
        }


class _Bars:
    async def window(self, symbol, end_us, count):
        return []


class _Feats:
    pass


class _PgForReserve:
    def __init__(self, held_usd: float):
        self.held_usd = float(held_usd)

    async def fetchall(self, sql: str):
        if "core.core_order_reservation" in sql:
            if self.held_usd <= 0:
                return []
            return [
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "reserved_usd": self.held_usd,
                }
            ]
        if "FROM core.core_order" in sql:
            return []
        return []


class _PgExec:
    def __init__(self):
        self.execute = AsyncMock(return_value=None)
        self.fetchall = AsyncMock(return_value=[])

    async def fetchone(self, sql: str, *args):
        if "FROM core.core_position" in sql:
            return {"pos_qty": 0.0}
        if "SELECT order_id::text AS order_id" in sql:
            return {"order_id": "oid-1", "broker_order_id": "101", "parent_order_id": None}
        return None


def _mk_env(decision_id: str, qty: float) -> DecisionEnvelope:
    return DecisionEnvelope(
        decision_id=decision_id,
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=float(qty),
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=10000.0,
        extras={"ref_px": 100.0, "edge_bp": 10.0},
    )


async def case_shadow_path_releases_arbiter_allocation() -> CaseResult:
    ledger = ReserveLedger(_PgForReserve(held_usd=0.0))
    pipe = DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=_Feats(),
        reserve_ledger=ledger,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
        arbiter_cfg={
            "arbiter": {
                "policy": "priority_then_marginal",
                "preempt": "never",
                "coexist_same_side": True,
                "ttl_ms": 1500,
                "symbol_cap": {"max_notional_usd": 10000, "max_participation": 0.0},
                "strategy_priority": {"qqq_intraday_001": 50},
                "scoring": {"w_edge": 1.0, "w_cost": 1.0, "penalty_bp": 0.0},
            }
        },
    )
    pipe.arbiter.finalize = AsyncMock(return_value=None)
    out = await pipe.process_decision(_mk_env("d-shadow-1", 100.0))
    if out.status != "APPROVED":
        return CaseResult("shadow_finalize", False, f"unexpected status={out.status}")
    try:
        pipe.arbiter.finalize.assert_awaited_once_with("d-shadow-1", "SHADOW_APPROVED")
    except AssertionError as exc:
        return CaseResult("shadow_finalize", False, str(exc))
    return CaseResult("shadow_finalize", True)


async def case_symbol_cap_uses_reservation_ledger() -> CaseResult:
    # Fully consumed by HELD reservation -> should be DEFERRED symbol_cap_exhausted.
    ledger = ReserveLedger(_PgForReserve(held_usd=10000.0))
    pipe = DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=_Feats(),
        reserve_ledger=ledger,
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=False,
        arbiter_cfg={
            "arbiter": {
                "policy": "priority_then_marginal",
                "preempt": "never",
                "coexist_same_side": True,
                "ttl_ms": 1500,
                "symbol_cap": {"max_notional_usd": 10000, "max_participation": 0.0},
                "strategy_priority": {"qqq_intraday_001": 50},
                "scoring": {"w_edge": 1.0, "w_cost": 1.0, "penalty_bp": 0.0},
            }
        },
    )
    out = await pipe.process_decision(_mk_env("d-cap-1", 100.0))
    if out.status != "DEFERRED":
        return CaseResult("symbol_cap_from_reservation", False, f"expected DEFERRED got {out.status}")
    reasons = out.reasons or []
    if "symbol_cap_exhausted" not in reasons:
        return CaseResult("symbol_cap_from_reservation", False, f"unexpected reasons={reasons}")
    return CaseResult("symbol_cap_from_reservation", True)


async def case_minimal_full_chain_pipeline_to_execution() -> CaseResult:
    # Empty reservation -> admit.
    ledger = ReserveLedger(_PgForReserve(held_usd=0.0))
    pipe = DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=_Feats(),
        reserve_ledger=ledger,
        shadow_mode=False,
        bypass_validator=True,
        bypass_arbiter=False,
        arbiter_cfg={
            "arbiter": {
                "policy": "priority_then_marginal",
                "preempt": "never",
                "coexist_same_side": True,
                "ttl_ms": 1500,
                "symbol_cap": {"max_notional_usd": 10000, "max_participation": 0.0},
                "strategy_priority": {"qqq_intraday_001": 50},
                "scoring": {"w_edge": 1.0, "w_cost": 1.0, "penalty_bp": 0.0},
            }
        },
    )

    pg = _PgExec()
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
    pipe.set_execution_service(svc)

    out = await pipe.process_decision(_mk_env("d-full-1", 100.0))
    if out.status != "APPROVED":
        return CaseResult("full_chain_pipeline_execution", False, f"unexpected status={out.status}")
    if svc._place_order.await_count != 1:
        return CaseResult("full_chain_pipeline_execution", False, "order placement was not executed")
    return CaseResult("full_chain_pipeline_execution", True)


async def main() -> int:
    cases = [
        case_shadow_path_releases_arbiter_allocation,
        case_symbol_cap_uses_reservation_ledger,
        case_minimal_full_chain_pipeline_to_execution,
    ]
    results: list[CaseResult] = []
    for fn in cases:
        try:
            results.append(await fn())
        except Exception as exc:
            results.append(CaseResult(fn.__name__, False, f"exception={exc}"))

    print("=== Decision/Execution Chain Regression Smoke ===")
    failed = 0
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"{status} | {r.name}" + (f" | {r.detail}" if r.detail else ""))
        if not r.ok:
            failed += 1
    print(f"SUMMARY | total={len(results)} failed={failed} passed={len(results)-failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
