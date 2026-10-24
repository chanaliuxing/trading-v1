from strategies.qqq_intraday_entry_block_logic import (
    block_stage_domain,
    blocked_preview_payload,
    gate_evaluated_payload,
    normalize_gate_details,
)


def test_gate_evaluated_payload_fields() -> None:
    payload = gate_evaluated_payload(
        symbol="qqq",
        exec_symbol="tqqq",
        side="long",
        qty=8,
        gate_allowed=False,
        gate_details={"a": 1},
        block_stage="entry_blocked_gate",
        trader_reason_code="signal_inactive_or_invalid",
    )
    assert payload["signal_symbol"] == "QQQ"
    assert payload["exec_symbol"] == "TQQQ"
    assert payload["side"] == "LONG"
    assert payload["qty"] == 8
    assert payload["gate_allowed"] is False
    assert payload["trader_reason_code"] == "SIGNAL_INACTIVE_OR_INVALID"


def test_blocked_preview_payload_fields() -> None:
    payload = blocked_preview_payload(
        reason="outside_ab_window",
        trader_reason_code="outside_ab_window",
        gate_details={"k": "v"},
        exec_symbol="TQQQ",
    )
    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "outside_ab_window"
    assert payload["trader_reason_code"] == "OUTSIDE_AB_WINDOW"
    assert payload["gate_details"] == {"k": "v"}
    assert payload["exec_symbol"] == "TQQQ"


def test_block_stage_domain() -> None:
    assert block_stage_domain("entry_blocked_reentry_cooldown") == "recon"
    assert block_stage_domain("entry_blocked_gate") == "strategy"


def test_normalize_gate_details_adds_meta_keys() -> None:
    payload = normalize_gate_details(
        {"seed_sample_count": 8},
        block_stage="entry_blocked_gate",
        reason="first_entry_seed_not_ready",
        trader_reason_code="SIGNAL_GATE_FAIL_FIRST_ENTRY_SEED_NOT_READY",
    )
    assert payload["seed_sample_count"] == 8
    assert payload["_schema"] == "gate_details_v1"
    assert payload["_block_stage"] == "entry_blocked_gate"
    assert payload["_reason"] == "first_entry_seed_not_ready"
    assert payload["_trader_reason_code"] == "SIGNAL_GATE_FAIL_FIRST_ENTRY_SEED_NOT_READY"
