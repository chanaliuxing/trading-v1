from types import SimpleNamespace

from strategies.stocks_kernel.execution.kernel import EquityExecutionKernel
from strategies.stocks_kernel.modules.broker_truth_sync import BrokerTruthSyncModule


def test_broker_truth_sync_detects_orphan_broker_order():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    module = BrokerTruthSyncModule()

    report = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="SOXL", order_ref="stkv1:alpha:SOXL:ENTRY:x", broker_order_id="1", perm_id=11),
        ),
        tracked_symbols=("SOXL",),
    )

    assert report.ok is False
    assert report.orphan_order_refs == ("stkv1:alpha:SOXL:ENTRY:x",)
    assert report.freeze_symbols == ("SOXL",)


def test_broker_truth_sync_accepts_unbound_own_protection_order_ref():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    module = BrokerTruthSyncModule()

    report = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="sl:DU123|alpha|NVDA|", broker_order_id="1", perm_id=11),
            SimpleNamespace(symbol="NVDA", order_ref="tp:DU123|alpha|NVDA|", broker_order_id="2", perm_id=12),
        ),
        tracked_symbols=("NVDA",),
    )

    assert report.ok is True
    assert report.orphan_order_refs == ()
    assert report.freeze_symbols == ()


def test_broker_truth_sync_ignores_foreign_protection_order_ref_same_symbol():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    module = BrokerTruthSyncModule()

    report = module.evaluate(
        strategy_id="beta",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="sl:DU123|alpha|NVDA|", broker_order_id="1", perm_id=11),
        ),
        tracked_symbols=("NVDA",),
    )

    assert report.ok is True
    assert report.orphan_order_refs == ()
    assert report.freeze_symbols == ()


def test_broker_truth_sync_malformed_protection_ref_remains_orphan():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    module = BrokerTruthSyncModule()

    report = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="sl:NVDA", broker_order_id="1", perm_id=11),
        ),
        tracked_symbols=("NVDA",),
    )

    assert report.ok is False
    assert report.orphan_order_refs == ("sl:NVDA",)
    assert report.freeze_symbols == ("NVDA",)


def test_broker_truth_sync_mismatched_protection_symbol_remains_orphan():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    module = BrokerTruthSyncModule()

    report = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="sl:DU123|alpha|TSLA|", broker_order_id="1", perm_id=11),
        ),
        tracked_symbols=("NVDA",),
    )

    assert report.ok is False
    assert report.orphan_order_refs == ("sl:DU123|alpha|TSLA|",)
    assert report.freeze_symbols == ("NVDA",)


def test_broker_truth_sync_entry_ref_still_requires_binding():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    module = BrokerTruthSyncModule()

    report = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="stkv1:alpha:NVDA:ENTRY:x", broker_order_id="1", perm_id=11),
        ),
        tracked_symbols=("NVDA",),
    )

    assert report.ok is False
    assert report.orphan_order_refs == ("stkv1:alpha:NVDA:ENTRY:x",)
    assert report.freeze_symbols == ("NVDA",)


def test_broker_truth_sync_requires_two_misses_before_escalating_missing():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    opened = kernel.open_entry_lot(strategy_id="alpha", symbol="SOXL", side="LONG", decision_id="d1")
    kernel.acknowledge_order(order_ref=opened.order_ref, broker_order_id="1", perm_id=11)
    module = BrokerTruthSyncModule()

    first = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(),
        tracked_symbols=("SOXL",),
    )
    second = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(),
        tracked_symbols=("SOXL",),
    )

    assert first.missing_order_refs == ()
    assert second.missing_order_refs == (opened.order_ref,)


def test_broker_truth_sync_does_not_freeze_restart_pending_entry_when_owner_protection_exists():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    opened = kernel.open_entry_lot(strategy_id="alpha", symbol="NVDA", side="LONG", decision_id="d1")
    kernel.acknowledge_order(order_ref=opened.order_ref, broker_order_id="1", perm_id=11)
    module = BrokerTruthSyncModule()

    first = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="sl:DU123|alpha|NVDA|", broker_order_id="10", perm_id=110),
            SimpleNamespace(symbol="NVDA", order_ref="tp:DU123|alpha|NVDA|", broker_order_id="11", perm_id=111),
        ),
        tracked_symbols=("NVDA",),
    )
    second = module.evaluate(
        strategy_id="alpha",
        kernel=kernel,
        broker_open_orders=(
            SimpleNamespace(symbol="NVDA", order_ref="sl:DU123|alpha|NVDA|", broker_order_id="10", perm_id=110),
            SimpleNamespace(symbol="NVDA", order_ref="tp:DU123|alpha|NVDA|", broker_order_id="11", perm_id=111),
        ),
        tracked_symbols=("NVDA",),
    )

    assert first.ok is True
    assert second.ok is True
    assert second.missing_order_refs == ()
    assert second.freeze_symbols == ()
