from services.ibkr.ib_request_governor import IBRequestGovernor


def test_governor_blocks_business_requests_during_market_revalidation():
    governor = IBRequestGovernor(
        market_health_snapshot=lambda: {"degraded": False, "revalidate_required": True},
        recovery_quiet_active=lambda: False,
    )

    decision = governor.allow(kind="option_chain", key="QQQ:0:C", requester="options_data")

    assert decision.allowed is False
    assert decision.reason == "market_revalidation_required"


def test_governor_allows_diagnostic_request_during_market_revalidation():
    governor = IBRequestGovernor(
        market_health_snapshot=lambda: {"degraded": True, "revalidate_required": True},
        recovery_quiet_active=lambda: False,
    )

    decision = governor.allow(
        kind="market_revalidation_probe",
        key="QQQ",
        requester="ib_connection_manager",
        diagnostic=True,
    )

    assert decision.allowed is True


def test_governor_blocks_business_requests_during_recovery_quiet_period():
    governor = IBRequestGovernor(
        market_health_snapshot=lambda: {"degraded": False, "revalidate_required": False},
        recovery_quiet_active=lambda: True,
    )

    decision = governor.allow(kind="symbol_resubscribe", key="QQQ", requester="ib_market_data")

    assert decision.allowed is False
    assert decision.reason == "recovery_quiet_period"


def test_governor_opens_circuit_after_repeated_failures():
    now = {"ts": 1000.0}
    governor = IBRequestGovernor(
        market_health_snapshot=lambda: {"degraded": False, "revalidate_required": False},
        recovery_quiet_active=lambda: False,
        now_fn=lambda: now["ts"],
    )

    governor.record_result(kind="option_chain", key="QQQ:0:C", ok=False, reason="empty_secdef_chain")
    governor.record_result(kind="option_chain", key="QQQ:0:C", ok=False, reason="empty_secdef_chain")
    assert governor.allow(kind="option_chain", key="QQQ:0:C", requester="options_data").allowed is True

    governor.record_result(kind="option_chain", key="QQQ:0:C", ok=False, reason="empty_secdef_chain")
    decision = governor.allow(kind="option_chain", key="QQQ:0:C", requester="options_data")

    assert decision.allowed is False
    assert decision.reason == "circuit_open:empty_secdef_chain"
