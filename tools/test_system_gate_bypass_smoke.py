#!/usr/bin/env python3
"""
Smoke tests for system-level gate bypass flow.

This script verifies:
1) Decision pipeline bypasses validator/arbiter and still returns APPROVED.
2) Translator can respect strategy order_style=MKT.
3) Translator can downgrade LMT/STP_LMT to MKT when L1 is missing.
4) NoopRiskService always approves.

Run:
  python3 tools/test_system_gate_bypass_smoke.py
"""
from __future__ import annotations

import asyncio
import time
import uuid
import sys
import types
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import prometheus_client  # type: ignore  # noqa: F401
except Exception:
    class _Metric:
        def labels(self, **kwargs):
            return self
        def inc(self, value: float = 1.0) -> None:
            return None
        def set(self, value: float) -> None:
            return None
        def time(self):
            class _Ctx:
                def __enter__(self):
                    return self
                def __exit__(self, exc_type, exc, tb):
                    return False
            return _Ctx()

    fake = types.ModuleType("prometheus_client")
    fake.Counter = lambda *a, **k: _Metric()
    fake.Histogram = lambda *a, **k: _Metric()
    fake.Gauge = lambda *a, **k: _Metric()
    sys.modules["prometheus_client"] = fake

# Mock ClickHouse pool used by translator fallback to avoid optional dependency imports.
if "libs.db.ch_pool" not in sys.modules:
    ch_pool_fake = types.ModuleType("libs.db.ch_pool")

    class _FakeCHClient:
        def query(self, sql: str, parameters: Dict[str, Any] | None = None):
            return [{"close": 100.0}]

    ch_pool_fake.get_client = lambda: _FakeCHClient()
    sys.modules["libs.db.ch_pool"] = ch_pool_fake

from libs.strategy.base import DecisionEnvelope, Freshness, OrderStyle

# Bypass services.decision.__init__ (which imports optional DB providers).
if "services.decision" not in sys.modules:
    decision_pkg = types.ModuleType("services.decision")
    decision_pkg.__path__ = [str(ROOT / "services" / "decision")]  # type: ignore[attr-defined]
    sys.modules["services.decision"] = decision_pkg

DecisionPipelineService = importlib.import_module("services.decision.pipeline").DecisionPipelineService

# Bypass heavy imports for services.risk.risk_service (DB deps) - only RiskDecision is needed.
if "services.risk.risk_service" not in sys.modules:
    risk_mod = types.ModuleType("services.risk.risk_service")

    @dataclass
    class RiskDecision:
        decision_id: str
        approved: bool
        reason: str
        hits: List[Dict[str, Any]]
        reserved_amount: float | None = None
        strategy_cap_used: float | None = None
        ts_us: int = 0

    risk_mod.RiskDecision = RiskDecision
    sys.modules["services.risk.risk_service"] = risk_mod

from services.risk.noop_risk_service import NoopRiskService


class DummyMarketStateProvider:
    def __init__(self, market_state: Dict[str, Any]) -> None:
        self._market_state = dict(market_state)

    async def get(self, symbol: str) -> Dict[str, Any]:
        out = dict(self._market_state)
        out.setdefault("symbol", symbol)
        out.setdefault("tick_size", 0.01)
        out.setdefault("ssr_active", False)
        out.setdefault("halted", False)
        return out


class DummyBarStore:
    async def window(self, symbol: str, end_us: int, count: int) -> List[Dict[str, Any]]:
        # Enough data for validator when needed.
        return [{"atr_bp": 10.0, "volume": 1000.0, "quality": "GOOD"} for _ in range(max(1, count))]


class DummyFeatureStore:
    pass


class DummyReserveLedger:
    async def snapshot(self) -> Dict[str, Any]:
        return {"items": {}}


class DummyADVProvider:
    async def adv_usd(self, symbol: str) -> float:
        return 1_000_000_000.0


class DummyTickSizeProvider:
    async def tick_size(self, symbol: str) -> float:
        return 0.01


def _mk_env(symbol: str, qty: float, style: OrderStyle) -> DecisionEnvelope:
    now_us = int(time.time() * 1_000_000)
    return DecisionEnvelope(
        decision_id=f"smoke-{uuid.uuid4().hex[:10]}",
        strategy_id="qqq_intraday_001",
        symbol=symbol,
        ts_bar_end_us=now_us,
        target_qty=qty,
        freshness=Freshness(dlb_ms=60_000, ttl_ms=60_000),
        tif="DAY",
        order_style=style,
        max_notional=10_000.0,
        signal_score=0.8,
        extras={"reason": "smoke_test"},
    )


async def _run() -> None:
    ms_missing = DummyMarketStateProvider(
        {
            "bid_px": 0.0,
            "ask_px": 0.0,
            "mid_px": 0.0,
            "last_px": 0.0,
        }
    )
    pipeline = DecisionPipelineService(
        market_state_provider=ms_missing,
        bar_store=DummyBarStore(),
        feature_store=DummyFeatureStore(),
        reserve_ledger=DummyReserveLedger(),
        adv_provider=DummyADVProvider(),
        tick_size_provider=DummyTickSizeProvider(),
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=True,
        translator_cfg={
            "respect_order_style": True,
            "fallback_to_market_on_missing_l1": True,
        },
    )

    # Case 1: Strategy MKT should remain MKT.
    env_mkt = _mk_env("TQQQ", 12.0, OrderStyle(kind="MKT"))
    r1 = await pipeline.process_decision(env_mkt)
    assert r1.status == "APPROVED", f"expected APPROVED, got {r1.status}"
    assert r1.plan is not None, "plan missing for approved decision"
    assert r1.plan.order_type == "MKT", f"expected MKT, got {r1.plan.order_type}"
    assert abs(float(r1.plan.qty) - 12.0) < 1e-9, f"qty mismatch: {r1.plan.qty}"

    # Case 2: Strategy LMT + missing L1 should safely downgrade to MKT.
    # Use None (not 0.0) to avoid translator's bar-fallback branch and directly
    # exercise the downgrade path.
    ms_none = DummyMarketStateProvider(
        {
            "bid_px": None,
            "ask_px": None,
            "mid_px": None,
            "last_px": None,
        }
    )
    pipeline2 = DecisionPipelineService(
        market_state_provider=ms_none,
        bar_store=DummyBarStore(),
        feature_store=DummyFeatureStore(),
        reserve_ledger=DummyReserveLedger(),
        adv_provider=DummyADVProvider(),
        tick_size_provider=DummyTickSizeProvider(),
        shadow_mode=True,
        bypass_validator=True,
        bypass_arbiter=True,
        translator_cfg={
            "respect_order_style": True,
            "fallback_to_market_on_missing_l1": True,
        },
    )
    env_lmt = _mk_env("SQQQ", 9.0, OrderStyle(kind="LMT"))
    r2 = await pipeline2.process_decision(env_lmt)
    assert r2.status == "APPROVED", f"expected APPROVED, got {r2.status}"
    assert r2.plan is not None, "plan missing for approved decision"
    assert r2.plan.order_type == "MKT", f"expected downgraded MKT, got {r2.plan.order_type}"

    # Case 3: NoopRiskService always approves.
    risk = NoopRiskService()
    rd = await risk.approve(
        envelope={"decision_id": "d1", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ"},
        idempotency_key="smoke-idemp-1",
    )
    assert rd.approved is True, "NoopRiskService should always approve"
    assert rd.reason == "risk_bypass_enabled", f"unexpected reason: {rd.reason}"
    released = await risk.release("smoke-idemp-1")
    assert released is True, "NoopRiskService release should return True"

    print("PASS: system gate bypass smoke test")


if __name__ == "__main__":
    asyncio.run(_run())
