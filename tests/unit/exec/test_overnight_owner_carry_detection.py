from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.production.position_safety_supervisor import PositionSafetySupervisor
from services.reconciliation.position_reconciliation import (
    BrokerAccountPosition,
    StrategyVirtualPosition,
    detect_overnight_carry_after_restart,
)


ACCOUNT = "DU123"
NOW = datetime.fromisoformat("2026-05-12T09:00:00-04:00")


def _broker(symbol: str = "SPY", qty: str = "100") -> BrokerAccountPosition:
    return BrokerAccountPosition(account_id=ACCOUNT, symbol=symbol, quantity=Decimal(qty), generated_at=NOW)


def _virtual(
    strategy_id: str = "strategy_a",
    strategy_instance_id: str = "strategy_a_1",
    symbol: str = "SPY",
    lot_id: str = "lot-a",
    qty: str = "100",
) -> StrategyVirtualPosition:
    return StrategyVirtualPosition(
        account_id=ACCOUNT,
        strategy_id=strategy_id,
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        lot_id=lot_id,
        quantity=Decimal(qty),
    )


def test_overnight_owner_resolved_position_detected_after_restart():
    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker()],
        virtual_positions=[_virtual()],
        strategy_policies={"strategy_a_1": {"hard_no_overnight": True}},
        now=NOW,
    )

    assert len(incidents) == 1
    assert incidents[0].status.value == "OWNER_RESOLVED"
    assert incidents[0].strategy_instance_id == "strategy_a_1"
    assert incidents[0].lot_id == "lot-a"


def test_overnight_owner_resolved_blocks_new_entry_but_allows_close_only():
    incident = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker()],
        virtual_positions=[_virtual()],
        strategy_policies={"strategy_a": {"intraday_flat_required": True}},
        now=NOW,
    )[0]

    assert incident.blocks_new_entry is True
    assert incident.close_only_allowed is True
    assert incident.requires_manual_review is False


def test_overnight_position_without_owner_goes_unknown_owner_manual_review():
    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker()],
        virtual_positions=[],
        strategy_policies={"active_strategy_same_symbol": {"hard_no_overnight": True}},
        now=NOW,
    )

    assert incidents[0].status.value == "UNKNOWN_OWNER"
    assert incidents[0].strategy_instance_id is None
    assert incidents[0].requires_manual_review is True


def test_overnight_ambiguous_owner_goes_manual_review():
    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker(qty="200")],
        virtual_positions=[
            _virtual("strategy_a", "strategy_a_1", "SPY", "lot-a", "100"),
            _virtual("strategy_b", "strategy_b_1", "SPY", "lot-b", "50"),
        ],
        strategy_policies={},
        now=NOW,
    )

    assert incidents[0].status.value == "AMBIGUOUS_OWNER"
    assert incidents[0].requires_manual_review is True


def test_overnight_broker_truth_missing_fails_closed():
    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=None,
        virtual_positions=[],
        strategy_policies={},
        now=NOW,
        broker_truth_available=False,
    )

    assert incidents[0].status.value == "BROKER_TRUTH_MISSING"
    assert incidents[0].blocks_new_entry is True


def test_overnight_broker_aggregate_mismatch_does_not_overwrite_virtual_positions():
    virtual_positions = [_virtual(qty="100")]

    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker(qty="125")],
        virtual_positions=virtual_positions,
        strategy_policies={},
        now=NOW,
    )

    assert incidents[0].status.value == "MANUAL_REVIEW_REQUIRED"
    assert virtual_positions[0].quantity == Decimal("100")


def test_overnight_detection_does_not_assign_to_active_strategy_by_symbol():
    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker(symbol="SPY", qty="10")],
        virtual_positions=[],
        strategy_policies={"active_spy_strategy": {"hard_no_overnight": True}},
        now=NOW,
    )

    assert incidents[0].status.value == "UNKNOWN_OWNER"
    assert incidents[0].strategy_id is None
    assert incidents[0].strategy_instance_id is None


def test_overnight_detection_does_not_auto_flatten_account():
    incidents = detect_overnight_carry_after_restart(
        account_id=ACCOUNT,
        broker_positions=[_broker()],
        virtual_positions=[],
        strategy_policies={},
        now=NOW,
    )

    assert incidents[0].reason == "overnight_broker_position_without_virtual_owner"
    assert incidents[0].requires_manual_review is True


class _Pg:
    def __init__(self):
        self.executed = []

    async def fetchall(self, sql, *args):
        text = str(sql)
        if "FROM core.position_ledger_current" in text:
            return [
                {
                    "account_id": ACCOUNT,
                    "strategy_id": "strategy_a",
                    "strategy_instance_id": "strategy_a_1",
                    "symbol": "SPY",
                    "lot_id": "lot-a",
                    "qty": 100.0,
                }
            ]
        return []

    async def fetchone(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((str(sql), args))
        return "OK"


class _LockStore:
    async def get_active(self, *, strategy_id: str, symbol: str):
        return None

    async def lock(self, **kwargs):
        return True

    async def acquire(self, **kwargs):
        return SimpleNamespace(lock_id="lock-1")

    async def unlock(self, **kwargs):
        return True


@pytest.mark.asyncio
async def test_overnight_incident_included_in_position_safety_artifact(tmp_path):
    snapshot = {
        "snapshot_id": "snap-overnight",
        "artifact_type": "live_broker_truth_snapshot",
        "artifact_present": True,
        "generated_at": NOW.isoformat(),
        "source": "live_broker",
        "broker_truth_backed": True,
        "account": ACCOUNT,
        "account_invalid": False,
        "lookup_failed": False,
        "stale": False,
        "positions": [{"account": ACCOUNT, "symbol": "SPY", "qty": 100.0, "market_ref_px": 100.0}],
        "open_orders": [],
    }
    supervisor = PositionSafetySupervisor(
        pg=_Pg(),
        ib_adapter=SimpleNamespace(account=ACCOUNT),
        manual_intervention_lock_store=_LockStore(),
        strategy_data_contracts=[
            SimpleNamespace(
                strategy_id="strategy_a",
                strategy_instance_id="strategy_a_1",
                symbols=("SPY",),
                hard_no_overnight=True,
            )
        ],
        live_broker_snapshot_provider=snapshot,
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=NOW)

    assert artifact["overnight_carry"]["detected"] is True
    assert artifact["overnight_carry"]["owner_resolved_count"] == 1
    assert artifact["overnight_carry"]["incidents"][0]["strategy_instance_id"] == "strategy_a_1"
    assert "overnight_carry" in artifact["blockers"]


@pytest.mark.asyncio
async def test_overnight_incident_blocks_production_state_until_resolved(tmp_path):
    supervisor = PositionSafetySupervisor(
        pg=_Pg(),
        ib_adapter=SimpleNamespace(account=ACCOUNT),
        manual_intervention_lock_store=_LockStore(),
        strategy_data_contracts=[SimpleNamespace(strategy_id="strategy_a", strategy_instance_id="strategy_a_1", hard_no_overnight=True)],
        live_broker_snapshot_provider={
            "snapshot_id": "snap-overnight",
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "generated_at": NOW.isoformat(),
            "source": "live_broker",
            "broker_truth_backed": True,
            "account": ACCOUNT,
            "account_invalid": False,
            "lookup_failed": False,
            "stale": False,
            "positions": [{"account": ACCOUNT, "symbol": "SPY", "qty": 100.0}],
            "open_orders": [],
        },
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=NOW)

    assert artifact["go_no_go"] == "no_go"
    assert artifact["overnight_carry"]["incident_count"] == 1
