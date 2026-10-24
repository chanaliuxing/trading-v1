from __future__ import annotations

import pytest

from services.exec.protection_lifecycle import (
    ProtectionLifecycleState,
    new_protection_lifecycle,
    protection_verification_ready,
    registry_row_to_lifecycle_record,
    transition_protection_lifecycle,
)


def test_protection_lifecycle_reaches_verified_with_owner_broker_and_core() -> None:
    record = new_protection_lifecycle(
        protection_group_id="pg-1",
        strategy_id="nvda_minute_flip_paper_001",
        strategy_instance_id="inst-1",
        symbol="NVDA",
        qty=1,
        side="BUY",
        owner_lane={"strategy_id": "nvda_minute_flip_paper_001", "lot_id": "lot-1"},
    )

    record = transition_protection_lifecycle(record, ProtectionLifecycleState.ENTRY_SUBMITTED)
    record = transition_protection_lifecycle(record, ProtectionLifecycleState.ENTRY_FILLED)
    record = transition_protection_lifecycle(record, ProtectionLifecycleState.PROTECTION_REQUIRED)
    record = transition_protection_lifecycle(record, ProtectionLifecycleState.PROTECTION_SUBMITTING)
    record = transition_protection_lifecycle(
        record,
        ProtectionLifecycleState.PROTECTION_BROKER_ACKED,
        broker_order_ids=("sl-1", "tp-1"),
        perm_ids=("101", "102"),
    )
    record = transition_protection_lifecycle(
        record,
        ProtectionLifecycleState.PROTECTION_CORE_PERSISTED,
        core_order_ids=("core-sl-1", "core-tp-1"),
    )
    assert protection_verification_ready(record) is True
    record = transition_protection_lifecycle(record, ProtectionLifecycleState.PROTECTION_VERIFIED)

    assert record.verified is True
    assert record.generation == 7
    assert record.broker_order_ids == ("sl-1", "tp-1")
    assert record.core_order_ids == ("core-sl-1", "core-tp-1")


def test_protection_lifecycle_rejects_unexpected_regression() -> None:
    record = new_protection_lifecycle(
        protection_group_id="pg-1",
        strategy_id="s1",
        strategy_instance_id="i1",
        symbol="NVDA",
        qty=1,
        side="BUY",
    )
    record = transition_protection_lifecycle(record, ProtectionLifecycleState.PROTECTION_REQUIRED)

    with pytest.raises(ValueError, match="protection_lifecycle_regression"):
        transition_protection_lifecycle(record, ProtectionLifecycleState.ENTRY_SUBMITTED)


def test_protection_lifecycle_allows_manual_review_escape() -> None:
    record = new_protection_lifecycle(
        protection_group_id="pg-1",
        strategy_id="s1",
        strategy_instance_id="i1",
        symbol="NVDA",
        qty=1,
        side="BUY",
    )
    record = transition_protection_lifecycle(record, ProtectionLifecycleState.PROTECTION_REQUIRED)
    record = transition_protection_lifecycle(
        record,
        ProtectionLifecycleState.QUARANTINED_MANUAL_REVIEW,
        metadata={"reason": "protection_timeout"},
    )

    assert record.state == "QUARANTINED_MANUAL_REVIEW"
    assert record.metadata["reason"] == "protection_timeout"


def test_registry_row_maps_to_lifecycle_record_without_new_schema() -> None:
    record = registry_row_to_lifecycle_record(
        {
            "protection_key": "DU123|s1|NVDA|lot-1|stop_loss|SELL|1|band:100",
            "strategy_id": "s1",
            "strategy_instance_id": "s1-inst",
            "symbol": "NVDA",
            "lot_id": "lot-1",
            "bracket_id": "bracket-1",
            "order_role": "STOP_LOSS",
            "protection_kind": "stop_loss",
            "side": "SELL",
            "desired_qty": 1,
            "broker_order_id": 123,
            "ib_perm_id": 456,
            "state": "ACTIVE",
            "updated_at": "2026-05-14T10:00:00-04:00",
        }
    )

    assert record.protection_group_id == "bracket-1"
    assert record.strategy_id == "s1"
    assert record.strategy_instance_id == "s1-inst"
    assert record.state == "PROTECTION_VERIFIED"
    assert record.verified is True
    assert record.broker_order_ids == ("123",)
    assert record.perm_ids == ("456",)
