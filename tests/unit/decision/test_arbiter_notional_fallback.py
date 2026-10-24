import pytest

from libs.strategy.base import CostEstimates, DecisionEnvelope, Freshness
from services.decision.arbiter import ArbitrationEngine


class _LedgerEmpty:
    async def snapshot(self):
        return {"items": {}}


class _LedgerUsed:
    def __init__(self, used_notional: float):
        self._used_notional = float(used_notional)

    async def snapshot(self):
        # Match arbiter expectation: per-item dict contains symbol + amount_remaining.
        return {
            "items": {
                "r1": {
                    "symbol": "TQQQ",
                    "amount_remaining": self._used_notional,
                }
            }
        }


def _mk_env(target_qty: float, max_notional: float = 0.0) -> DecisionEnvelope:
    env = DecisionEnvelope(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=target_qty,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=max_notional,
        extras={"ref_px": 100.0, "edge_bp": 10.0},
    )
    env.cost_estimates = CostEstimates(
        slippage_bp=1.0,
        fees_bp=0.1,
        impact_bp=0.2,
        confidence=0.9,
    )
    return env


@pytest.mark.asyncio
async def test_arbiter_does_not_reject_when_env_max_notional_is_zero():
    cfg = {
        "arbiter": {
            "ttl_ms": 1500,
            "symbol_cap": {"max_notional_usd": 500_000, "max_participation": 0.0},
            "strategy_priority": {"s1": 50},
            "coexist_same_side": True,
        }
    }
    arb = ArbitrationEngine(cfg=cfg, reserve_ledger=_LedgerEmpty(), adv_provider=None)

    ok, qty, meta = await arb.admit(_mk_env(target_qty=100.0, max_notional=0.0))

    assert ok is True
    assert qty == pytest.approx(100.0)
    assert meta.get("reason") == "first"


@pytest.mark.asyncio
async def test_arbiter_clips_qty_by_remaining_symbol_capacity_when_env_max_notional_is_zero():
    cfg = {
        "arbiter": {
            "ttl_ms": 1500,
            "symbol_cap": {"max_notional_usd": 15_000, "max_participation": 0.0},
            "strategy_priority": {"s1": 50},
            "coexist_same_side": True,
        }
    }
    # Used 5,000 already; remaining should be 10,000 => qty clip to 100 at $100 ref price.
    arb = ArbitrationEngine(cfg=cfg, reserve_ledger=_LedgerUsed(used_notional=5_000), adv_provider=None)

    ok, qty, meta = await arb.admit(_mk_env(target_qty=200.0, max_notional=0.0))

    assert ok is True
    assert qty == pytest.approx(100.0)
    assert meta.get("reason") == "first"


@pytest.mark.asyncio
async def test_arbiter_preserves_integral_stock_qty_after_notional_clip():
    cfg = {
        "arbiter": {
            "ttl_ms": 1500,
            "symbol_cap": {"max_notional_usd": 500_000, "max_participation": 0.0},
            "strategy_priority": {"s1": 50},
            "coexist_same_side": True,
        }
    }
    arb = ArbitrationEngine(cfg=cfg, reserve_ledger=_LedgerEmpty(), adv_provider=None)

    env = _mk_env(target_qty=253.0, max_notional=9968.2)
    env.symbol = "SOXS"
    env.extras = {
        "ref_px": 39.400000000000006,
        "edge_bp": 10.0,
        "instrument_type": "STK",
    }

    ok, qty, meta = await arb.admit(env)

    assert ok is True
    assert qty == pytest.approx(253.0)
    assert meta.get("normalized_allow_qty") == pytest.approx(253.0)


@pytest.mark.asyncio
async def test_arbiter_finalize_releases_only_decision_allocation():
    cfg = {
        "arbiter": {
            "ttl_ms": 1500,
            "symbol_cap": {"max_notional_usd": 500_000, "max_participation": 0.0},
            "strategy_priority": {"s1": 50},
            "coexist_same_side": True,
        }
    }
    arb = ArbitrationEngine(cfg=cfg, reserve_ledger=_LedgerEmpty(), adv_provider=None)

    env1 = _mk_env(target_qty=40.0, max_notional=0.0)
    env1.decision_id = "d1"
    ok1, qty1, _ = await arb.admit(env1)
    assert ok1 is True
    assert qty1 == pytest.approx(40.0)

    env2 = _mk_env(target_qty=60.0, max_notional=0.0)
    env2.decision_id = "d2"
    ok2, qty2, _ = await arb.admit(env2)
    assert ok2 is True
    assert qty2 == pytest.approx(60.0)

    await arb.finalize("d1", "SUBMITTED")

    active = arb._actives.get("TQQQ")
    assert active is not None
    assert active.winners.get("s1") == pytest.approx(60.0)
    assert active.total_qty == pytest.approx(60.0)

    await arb.finalize("d2", "SUBMITTED")
    assert arb._actives.get("TQQQ") is None
