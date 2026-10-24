from strategies.qqq_intraday_gate_audit_logic import (
    entry_block_payload,
    signal_block_reason,
    trader_reason_code,
)


def test_signal_block_reason_passthrough_and_default() -> None:
    assert signal_block_reason("sign_mismatch") == "sign_mismatch"
    assert signal_block_reason("unknown_reason") == "signal_inactive_or_invalid"


def test_trader_reason_code_mapping_and_fallback() -> None:
    assert trader_reason_code("market_data_unavailable") == "SIGNAL_GATE_FAIL_MARKET_DATA_UNAVAILABLE"
    assert trader_reason_code("fast_cooldown_active") == "FAST_COOLDOWN_ACTIVE"
    assert trader_reason_code("") == "UNKNOWN"


def test_entry_block_payload_normalized() -> None:
    payload = entry_block_payload(
        symbol="qqq",
        exec_symbol="tqqq",
        reason="sign_mismatch",
        trader_reason_code_value="sign_mismatch",
        gate_details={"a": 1},
    )
    assert payload["signal_symbol"] == "QQQ"
    assert payload["exec_symbol"] == "TQQQ"
    assert payload["gate_reason"] == "sign_mismatch"
    assert payload["trader_reason_code"] == "SIGN_MISMATCH"
    assert payload["gate_details"] == {"a": 1}
