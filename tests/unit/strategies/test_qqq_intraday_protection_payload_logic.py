from strategies.qqq_intraday_protection_payload_logic import (
    kill_switch_order_payload,
    protection_order_payload,
    protection_stop_rebuilt_payload,
)


def test_protection_order_payload() -> None:
    payload = protection_order_payload(exec_symbol="TQQQ", order_ref="oref", stop_px=48.5, live_qty=8)
    assert payload == {"exec_symbol": "TQQQ", "order_ref": "oref", "stop_px": 48.5, "live_qty": 8}


def test_kill_switch_order_payload_optional_qty() -> None:
    payload = kill_switch_order_payload(exec_symbol="SQQQ", order_ref="oref2", qty=6, live_qty=6)
    assert payload["qty"] == 6
    payload2 = kill_switch_order_payload(exec_symbol="SQQQ", order_ref=None, qty=None, live_qty=6)
    assert "qty" not in payload2


def test_protection_stop_rebuilt_payload() -> None:
    payload = protection_stop_rebuilt_payload(
        exec_symbol="TQQQ",
        entry_px=50.0,
        entry_atr_used=0.7,
        stop_px=48.5,
        stop_dist_pre=1.5,
    )
    assert payload["exec_symbol"] == "TQQQ"
    assert payload["entry_px"] == 50.0
    assert payload["entry_atr_used"] == 0.7
    assert payload["stop_px"] == 48.5
    assert payload["stop_dist_pre"] == 1.5
