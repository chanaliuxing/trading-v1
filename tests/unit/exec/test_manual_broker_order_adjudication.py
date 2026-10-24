from __future__ import annotations

from services.exec.models import (
    BrokerOrderAdjudicationStatus,
    OrderRole,
    adjudicate_broker_order_owner,
)


def _broker(**overrides):
    row = {
        "account_id": "DU123",
        "symbol": "SPY",
        "broker_order_id": "501",
        "ib_perm_id": 1001,
        "ib_client_id": 207,
        "ib_connection_epoch": 4,
        "order_ref": "owner-ref-a",
        "side": "BUY",
    }
    row.update(overrides)
    return row


def _owner(**overrides):
    row = {
        "account_id": "DU123",
        "strategy_id": "mean_revert",
        "strategy_instance_id": "mean_revert_a",
        "symbol": "SPY",
        "lot_id": "lot-a",
        "broker_order_id": "501",
        "ib_perm_id": 1001,
        "ib_client_id": 207,
        "ib_connection_epoch": 4,
        "client_order_id": "client-a",
        "intent_id": "intent-a",
        "order_ref": "owner-ref-a",
    }
    row.update(overrides)
    return row


def test_manual_broker_order_without_durable_owner_goes_manual_review():
    result = adjudicate_broker_order_owner(
        _broker(order_role=OrderRole.MANUAL_BROKER_ORDER.value),
        [],
    )

    assert result.status == BrokerOrderAdjudicationStatus.MANUAL_BROKER_ORDER
    assert result.requires_manual_review is True
    assert result.allow_strategy_attribution is False


def test_manual_broker_order_same_symbol_as_active_strategy_not_assigned():
    result = adjudicate_broker_order_owner(
        _broker(ib_perm_id=9999, broker_order_id="999", order_ref="manual-spy"),
        [],
        active_strategies=[{"strategy_instance_id": "active_spy", "symbol": "SPY"}],
    )

    assert result.status == BrokerOrderAdjudicationStatus.UNKNOWN_OWNER
    assert result.strategy_instance_id is None
    assert result.allow_strategy_attribution is False


def test_unknown_broker_order_not_bound_by_symbol_only():
    result = adjudicate_broker_order_owner(
        _broker(ib_perm_id=None, broker_order_id="", order_ref=""),
        [_owner(ib_perm_id=None, broker_order_id="", order_ref="different-ref")],
    )

    assert result.status == BrokerOrderAdjudicationStatus.UNKNOWN_OWNER
    assert result.allow_auto_cancel is False


def test_unknown_broker_order_not_bound_by_order_ref_prefix():
    result = adjudicate_broker_order_owner(
        _broker(ib_perm_id=None, ib_client_id=None, broker_order_id="", order_ref="owner"),
        [_owner(ib_perm_id=None, ib_client_id=None, broker_order_id="", order_ref="owner-ref-a")],
    )

    assert result.status == BrokerOrderAdjudicationStatus.UNKNOWN_OWNER


def test_unknown_broker_order_not_bound_by_order_ref_substring():
    result = adjudicate_broker_order_owner(
        _broker(ib_perm_id=None, ib_client_id=None, broker_order_id="", order_ref="ref"),
        [_owner(ib_perm_id=None, ib_client_id=None, broker_order_id="", order_ref="owner-ref-a")],
    )

    assert result.status == BrokerOrderAdjudicationStatus.UNKNOWN_OWNER


def test_unknown_broker_order_not_auto_cancelled():
    result = adjudicate_broker_order_owner(_broker(ib_perm_id=9999, broker_order_id="999"), [])

    assert result.status == BrokerOrderAdjudicationStatus.UNKNOWN_OWNER
    assert result.allow_auto_cancel is False


def test_unknown_broker_fill_not_attributed_to_strategy():
    result = adjudicate_broker_order_owner(_broker(ib_perm_id=9999, broker_order_id="999"), [])

    assert result.allow_strategy_attribution is False
    assert result.owner is None


def test_unknown_broker_fill_not_pnl_replay_ready():
    result = adjudicate_broker_order_owner(_broker(ib_perm_id=9999, broker_order_id="999"), [])

    assert result.allow_pnl_attribution is False


def test_foreign_client_order_goes_manual_review():
    result = adjudicate_broker_order_owner(
        _broker(ib_client_id=999),
        [_owner()],
        system_client_ids={207},
    )

    assert result.status == BrokerOrderAdjudicationStatus.FOREIGN_CLIENT_ORDER
    assert result.requires_manual_review is True


def test_ambiguous_owner_rows_go_manual_review():
    result = adjudicate_broker_order_owner(
        _broker(),
        [_owner(lot_id="lot-a"), _owner(lot_id="lot-b")],
    )

    assert result.status == BrokerOrderAdjudicationStatus.AMBIGUOUS_OWNER
    assert result.requires_manual_review is True
    assert result.allow_strategy_attribution is False


def test_exact_durable_owner_row_still_binds_system_order():
    result = adjudicate_broker_order_owner(_broker(), [_owner()])

    assert result.status == BrokerOrderAdjudicationStatus.BOUND_SYSTEM_OWNER
    assert result.strategy_instance_id == "mean_revert_a"
    assert result.allow_strategy_attribution is True
    assert result.allow_pnl_attribution is True


def test_unknown_broker_order_creates_owner_aware_quarantine_or_manual_review():
    result = adjudicate_broker_order_owner(_broker(ib_perm_id=9999, broker_order_id="999"), [])

    assert result.status == BrokerOrderAdjudicationStatus.UNKNOWN_OWNER
    assert result.reason == "UNKNOWN_OWNER_manual_required_no_exact_durable_owner_match"
    assert result.requires_manual_review is True
