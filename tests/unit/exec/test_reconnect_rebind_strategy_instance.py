from types import SimpleNamespace

from services.exec.adapters.ib_adapter import IBInsyncAdapter
from services.exec.models import RebindStatus, rebind_broker_order_to_owner


def _broker(**overrides):
    data = {
        "account_id": "DU123",
        "symbol": "SPY",
        "perm_id": 1001,
        "client_id": 207,
        "broker_order_id": "501",
        "order_ref": "exact-owner-ref",
        "ib_connection_epoch": 3,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _row(**overrides):
    data = {
        "account_id": "DU123",
        "strategy_id": "mean_revert",
        "strategy_instance_id": "mean_revert_a",
        "symbol": "SPY",
        "lot_id": "lot-a",
        "intent_id": "intent-a",
        "client_order_id": "client-a",
        "order_role": "ENTRY",
        "broker_order_id": "501",
        "ib_perm_id": 1001,
        "ib_client_id": 207,
        "ib_connection_epoch": 3,
        "order_ref": "exact-owner-ref",
    }
    data.update(overrides)
    return data


def test_reconnect_rebinds_by_perm_id_to_strategy_instance():
    result = rebind_broker_order_to_owner(_broker(), [_row()])

    assert result.status == RebindStatus.BOUND_OWNER
    assert result.owner["strategy_instance_id"] == "mean_revert_a"


def test_reconnect_same_symbol_two_strategy_orders_rebind_correct_instances():
    rows = [
        _row(strategy_instance_id="mean_revert_a", ib_perm_id=1001, broker_order_id="501"),
        _row(strategy_instance_id="mean_revert_b", ib_perm_id=1002, broker_order_id="502", order_ref="exact-owner-ref-b"),
    ]

    result_a = rebind_broker_order_to_owner(_broker(perm_id=1001, broker_order_id="501"), rows)
    result_b = rebind_broker_order_to_owner(_broker(perm_id=1002, broker_order_id="502", order_ref="exact-owner-ref-b"), rows)

    assert result_a.owner["strategy_instance_id"] == "mean_revert_a"
    assert result_b.owner["strategy_instance_id"] == "mean_revert_b"


def test_reconnect_order_ref_prefix_does_not_bind_owner():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=None, client_id=None, broker_order_id="", order_ref="exact"),
        [_row(ib_perm_id=None, ib_client_id=None, broker_order_id="", order_ref="exact-owner-ref")],
    )

    assert result.status == RebindStatus.UNKNOWN_OWNER


def test_reconnect_order_ref_substring_does_not_bind_owner():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=None, client_id=None, broker_order_id="", order_ref="owner"),
        [_row(ib_perm_id=None, ib_client_id=None, broker_order_id="", order_ref="exact-owner-ref")],
    )

    assert result.status == RebindStatus.UNKNOWN_OWNER


def test_reconnect_unknown_broker_order_becomes_unknown_owner():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=9999, broker_order_id="999", order_ref="unknown-ref"),
        [_row()],
    )

    assert result.status == RebindStatus.UNKNOWN_OWNER


def test_reconnect_ambiguous_perm_id_goes_manual_review():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=1001, client_id=207),
        [
            _row(strategy_instance_id="mean_revert_a"),
            _row(strategy_instance_id="mean_revert_b", client_order_id="client-b"),
        ],
    )

    assert result.status == RebindStatus.AMBIGUOUS_OWNER


def test_reconnect_broker_order_id_requires_client_id_and_epoch():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=None, client_id=None, broker_order_id="501", order_ref=""),
        [_row(ib_perm_id=None)],
    )

    assert result.status == RebindStatus.UNKNOWN_OWNER


def test_reconnect_manual_broker_order_not_assigned_to_active_strategy():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=None, client_id=None, broker_order_id="", order_ref="", order_role="MANUAL_BROKER_ORDER"),
        [_row()],
    )

    assert result.status == RebindStatus.MANUAL_REVIEW_REQUIRED


def test_reconnect_stale_epoch_order_not_bound_to_current_owner():
    result = rebind_broker_order_to_owner(
        _broker(perm_id=None, broker_order_id="501", client_id=207, ib_connection_epoch=4, order_ref=""),
        [_row(ib_perm_id=None, broker_order_id="501", ib_client_id=207, ib_connection_epoch=3)],
        current_epoch=4,
    )

    assert result.status == RebindStatus.STALE_EPOCH


def test_ib_adapter_rebind_open_order_owner_exposes_unknown_owner_classification():
    result = IBInsyncAdapter.rebind_open_order_owner(
        _broker(perm_id=None, client_id=None, broker_order_id="", order_ref=""),
        [_row()],
    )

    assert result.status == RebindStatus.UNKNOWN_OWNER
