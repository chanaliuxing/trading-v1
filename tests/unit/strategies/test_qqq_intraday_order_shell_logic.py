from types import SimpleNamespace

from strategies.qqq_intraday_order_shell_logic import (
    cancel_id_from_order,
    matches_any_namespace_prefix,
    terminal_order_state,
)


def test_terminal_order_state() -> None:
    assert terminal_order_state(SimpleNamespace(state="FILLED")) is True
    assert terminal_order_state(SimpleNamespace(state="PENDING_SUBMIT")) is False


def test_cancel_id_from_order_priority() -> None:
    order = SimpleNamespace(broker_order_id="123", order_id="222", id="333")
    assert cancel_id_from_order(order) == 123
    order2 = SimpleNamespace(broker_order_id=None, order_id="222", id="333")
    assert cancel_id_from_order(order2) == 222


def test_matches_any_namespace_prefix() -> None:
    prefixes = ["iswv2:index:TQQQ:", "iswv2:index:SQQQ:"]
    assert matches_any_namespace_prefix("iswv2:index:TQQQ:ENTRY:TRG:x", prefixes) is True
    assert matches_any_namespace_prefix("qqqv1:qqq_intraday:TQQQ:ENTRY:TRG:x", prefixes) is False
