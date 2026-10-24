import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.risk.risk_service import RiskService


class _DummyReservation:
    def __init__(self):
        self.calls = 0
        self.last_kwargs = None
        self.db = SimpleNamespace(fetchone=self._fetchone)

    async def approve_and_reserve(self, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return SimpleNamespace(approved=False, reserved_usd=0.0, reason="INSUFFICIENT_CAPITAL")

    async def _fetchone(self, _sql, _strategy_ids):
        return {"reserved_usd": 0.0, "position_notional_usd": 0.0}

    async def release(self, _idempotency_key: str):
        return True


class _DummyRegLite:
    def check(self, _intent, _snap):
        return []

class _DummyRegLiteBlocking:
    class _Hit:
        def to_dict(self):
            return {"rule": "no_nbbo", "reason": "MKT without NBBO snapshot"}

    def check(self, _intent, _snap):
        return [self._Hit()]


@pytest.mark.asyncio
async def test_flatten_action_skips_capital_reservation():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d1",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "target_qty": -100.0,
        "order_style": {"kind": "LMT", "limit_px": 50.0},
        "extras": {"flatten_action": True, "close_only": True},
    }
    out = await svc.approve(env, "idem1")
    assert out.approved is True
    assert out.reason == "approved_close_only_exempt"
    assert out.reserved_amount == 0.0
    assert reservation.calls == 0


@pytest.mark.asyncio
async def test_protective_stop_skips_capital_reservation():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d2",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "target_qty": -50.0,
        "order_style": {"kind": "STP", "stop_px": 49.5},
        "extras": {"protective_stop": True, "close_only": True},
    }
    out = await svc.approve(env, "idem2")
    assert out.approved is True
    assert out.reason == "approved_close_only_exempt"
    assert reservation.calls == 0


@pytest.mark.asyncio
async def test_entry_still_requires_reservation():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d3",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "target_qty": 100.0,
        "order_style": {"kind": "MKT"},
        "extras": {},
    }
    out = await svc.approve(env, "idem3")
    assert out.approved is False
    assert out.reason == "reservation_failed"
    assert reservation.calls == 1


@pytest.mark.asyncio
async def test_reservation_failure_reason_is_propagated():
    class _DummyReservationWithReason(_DummyReservation):
        async def approve_and_reserve(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(approved=False, reserved_usd=0.0, reason="STRATEGY_NOT_FOUND")

    reservation = _DummyReservationWithReason()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d4",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "target_qty": 10.0,
        "order_style": {"kind": "MKT"},
        "extras": {},
    }
    out = await svc.approve(env, "idem4")
    assert out.approved is False
    assert out.reason == "reservation_failed"
    assert out.hits and out.hits[0]["rule"] == "capital_reservation"
    assert out.hits[0]["reason"] == "STRATEGY_NOT_FOUND"


@pytest.mark.asyncio
async def test_manual_flatten_bypasses_reglite():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLiteBlocking(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d5",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "target_qty": -100.0,
        "order_style": {"kind": "MKT"},
        "extras": {"manual_intervention": True, "flatten_action": True, "close_only": True},
    }
    out = await svc.approve(env, "idem5")
    assert out.approved is True
    assert out.reason == "approved_close_only_exempt"
    assert reservation.calls == 0


@pytest.mark.asyncio
async def test_runtime_recovery_flatten_bypasses_reglite():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLiteBlocking(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "runtime_recovery:s1:OPT:QQQ:20260505:682:C:long:flatten",
        "strategy_id": "s1",
        "symbol": "OPT:QQQ:20260505:682:C",
        "target_qty": -2.0,
        "order_style": {"kind": "MKT"},
        "extras": {"runtime_recovery_action": True, "flatten_action": True, "close_only": True},
    }
    out = await svc.approve(env, "idem_runtime_recovery")
    assert out.approved is True
    assert out.reason == "approved_close_only_exempt"
    assert reservation.calls == 0


@pytest.mark.asyncio
async def test_non_manual_close_only_still_checked_by_reglite():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLiteBlocking(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d6",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "target_qty": -100.0,
        "order_style": {"kind": "MKT"},
        "extras": {"flatten_action": True, "close_only": True},
    }
    out = await svc.approve(env, "idem6")
    assert out.approved is False
    assert out.reason == "reglite_block"


@pytest.mark.asyncio
async def test_shared_cap_group_blocks_when_combined_usage_exceeds_limit():
    class _DummyReservationWithUsage(_DummyReservation):
        async def _fetchone(self, _sql, _strategy_ids):
            return {"reserved_usd": 2500.0, "position_notional_usd": 17250.0}

    reservation = _DummyReservationWithUsage()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d7",
        "strategy_id": "qqq_1dte_ma10_reject_0324_001",
        "symbol": "OPT:QQQ:20260326:C:530",
        "target_qty": 4.0,
        "order_style": {"kind": "MKT"},
        "extras": {
            "instrument_type": "OPTION",
            "ref_px": 1.5,
            "shared_cap_group_id": "QQQ_OPT_LIVE_SHARED",
            "shared_cap_group_limit_usd": 20000,
            "shared_cap_group_strategy_ids": [
                "qqq_0dte_opt_live_001",
                "qqq_1dte_ma10_reject_0324_001",
            ],
        },
    }
    out = await svc.approve(env, "idem7")
    assert out.approved is False
    assert out.reason == "shared_cap_group_block"
    assert out.hits and out.hits[0]["rule"] == "shared_cap_group_limit"
    assert reservation.calls == 0


@pytest.mark.asyncio
async def test_shared_cap_group_option_intent_notional_is_not_multiplied_twice():
    class _DummyReservationWithUsage(_DummyReservation):
        async def _fetchone(self, _sql, _strategy_ids):
            return {"reserved_usd": 0.0, "position_notional_usd": 0.0}

    reservation = _DummyReservationWithUsage()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
    )
    env = {
        "decision_id": "d8",
        "strategy_id": "qqq_0dte_opt_live_001",
        "symbol": "OPT:QQQ:20260325:585:C",
        "target_qty": 3.0,
        "order_style": {"kind": "MKT"},
        "extras": {
            "instrument_type": "OPTION",
            "contracts_mult": 100,
            "ref_px": 2.935,
            "intent_notional_usd": 880.5,
            "shared_cap_group_id": "QQQ_OPT_LIVE_SHARED",
            "shared_cap_group_limit_usd": 20000,
            "shared_cap_group_strategy_ids": [
                "qqq_0dte_opt_live_001",
                "qqq_1dte_ma10_reject_0324_001",
            ],
        },
    }
    out = await svc.approve(env, "idem8")
    assert out.reason == "reservation_failed"
    assert reservation.calls == 1


@pytest.mark.asyncio
async def test_reservation_uses_strategy_default_buffer_when_envelope_does_not_override():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
        default_buffer_bps=10,
    )
    env = {
        "decision_id": "d9",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "target_qty": 100.0,
        "order_style": {"kind": "LMT", "limit_px": 50.0},
        "extras": {},
    }
    await svc.approve(env, "idem9")
    assert reservation.calls == 1
    assert reservation.last_kwargs["buffer_bps"] is None


@pytest.mark.asyncio
async def test_reservation_preserves_explicit_zero_buffer_override():
    reservation = _DummyReservation()
    svc = RiskService(
        reservation_service=reservation,
        reg_lite_guard=_DummyRegLite(),
        capital_quota_service=None,
        market_state_provider=None,
        default_buffer_bps=10,
    )
    env = {
        "decision_id": "d10",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "target_qty": 100.0,
        "order_style": {"kind": "LMT", "limit_px": 50.0},
        "extras": {"slippage_buffer_bps": 0},
    }
    await svc.approve(env, "idem10")
    assert reservation.calls == 1
    assert reservation.last_kwargs["buffer_bps"] == 0
