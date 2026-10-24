from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from services.reconciliation.position_reconciliation import (
    BrokerAccountPosition,
    StrategyVirtualPosition,
    reconcile_broker_aggregate_to_virtual_positions,
)


def _broker(qty: str = "150") -> BrokerAccountPosition:
    return BrokerAccountPosition(
        account_id="A1",
        symbol="SPY",
        quantity=Decimal(qty),
        avg_cost=Decimal("500"),
        generated_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )


def _virtual(
    *,
    strategy_instance_id: str,
    lot_id: str,
    qty: str,
    strategy_id: str = "mean_revert",
) -> StrategyVirtualPosition:
    return StrategyVirtualPosition(
        account_id="A1",
        strategy_id=strategy_id,
        strategy_instance_id=strategy_instance_id,
        symbol="SPY",
        lot_id=lot_id,
        quantity=Decimal(qty),
    )


def test_broker_aggregate_matches_sum_of_virtual_positions_no_mutation() -> None:
    positions = [
        _virtual(strategy_instance_id="strat_a_1", lot_id="lot-a", qty="100"),
        _virtual(strategy_instance_id="strat_b_1", lot_id="lot-b", qty="50"),
    ]

    result = reconcile_broker_aggregate_to_virtual_positions(_broker("150"), positions)

    assert result.status == "MATCHED"
    assert result.matched is True
    assert result.delta == Decimal("0")
    assert result.must_not_mutate_virtual_positions is True
    assert result.affected_virtual_positions == positions


def test_broker_aggregate_mismatch_creates_report_not_virtual_overwrite() -> None:
    positions = [
        _virtual(strategy_instance_id="strat_a_1", lot_id="lot-a", qty="100"),
        _virtual(strategy_instance_id="strat_b_1", lot_id="lot-b", qty="50"),
    ]

    result = reconcile_broker_aggregate_to_virtual_positions(_broker("125"), positions)

    assert result.status == "MISMATCH"
    assert result.matched is False
    assert result.delta == Decimal("-25")
    assert result.must_not_mutate_virtual_positions is True
    assert [p.quantity for p in positions] == [Decimal("100"), Decimal("50")]


def test_broker_aggregate_without_virtual_owner_goes_unknown_owner() -> None:
    result = reconcile_broker_aggregate_to_virtual_positions(_broker("25"), [])

    assert result.status == "UNKNOWN_OWNER"
    assert result.delta == Decimal("25")
    assert result.must_not_mutate_virtual_positions is True


def test_broker_aggregate_does_not_assign_delta_to_active_strategy() -> None:
    active_strategy = _virtual(strategy_instance_id="active_strategy", lot_id="lot-active", qty="0")

    result = reconcile_broker_aggregate_to_virtual_positions(_broker("10"), [active_strategy])

    assert result.status == "MISMATCH"
    assert result.delta == Decimal("10")
    assert result.affected_virtual_positions[0].quantity == Decimal("0")


def test_broker_aggregate_does_not_clear_strategy_lots() -> None:
    positions = [_virtual(strategy_instance_id="strat_a_1", lot_id="lot-a", qty="100")]

    result = reconcile_broker_aggregate_to_virtual_positions(_broker("0"), positions)

    assert result.status == "MISMATCH"
    assert positions[0].quantity == Decimal("100")
    assert result.must_not_mutate_virtual_positions is True


def test_broker_aggregate_does_not_update_core_position_by_symbol_only() -> None:
    result = reconcile_broker_aggregate_to_virtual_positions(_broker("0"), [])

    assert result.status == "MATCHED"
    assert result.must_not_mutate_virtual_positions is True


def test_strategy_virtual_positions_remain_unchanged_after_reconcile() -> None:
    positions = [
        _virtual(strategy_instance_id="strat_a_1", lot_id="lot-a", qty="100"),
        _virtual(strategy_instance_id="strat_b_1", lot_id="lot-b", qty="50"),
    ]
    before = list(positions)

    reconcile_broker_aggregate_to_virtual_positions(_broker("151"), positions)

    assert positions == before


def test_broker_truth_stale_does_not_mutate_virtual_positions() -> None:
    positions = [_virtual(strategy_instance_id="strat_a_1", lot_id="lot-a", qty="100")]

    result = reconcile_broker_aggregate_to_virtual_positions(None, positions)

    assert result.status == "UNKNOWN_BROKER_TRUTH"
    assert result.broker_quantity is None
    assert result.virtual_quantity_sum == Decimal("100")
    assert result.must_not_mutate_virtual_positions is True


def test_position_reconciliation_result_contains_delta_and_owner_lanes() -> None:
    positions = [
        _virtual(strategy_instance_id="strat_a_1", lot_id="lot-a", qty="100"),
        _virtual(strategy_instance_id="strat_b_1", lot_id="lot-b", qty="50"),
    ]

    result = reconcile_broker_aggregate_to_virtual_positions(_broker("149"), positions)

    assert result.account_id == "A1"
    assert result.symbol == "SPY"
    assert result.broker_quantity == Decimal("149")
    assert result.virtual_quantity_sum == Decimal("150")
    assert result.delta == Decimal("-1")
    assert {p.strategy_instance_id for p in result.affected_virtual_positions} == {"strat_a_1", "strat_b_1"}
