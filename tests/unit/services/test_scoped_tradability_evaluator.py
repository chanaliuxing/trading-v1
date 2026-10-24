from __future__ import annotations

from services.production.scoped_tradability import (
    BlockerScopeType,
    FlowType,
    HaltClass,
    ScopedBlocker,
    SessionPolicy,
    SessionPolicyType,
    TradabilityInputs,
    TradabilityScope,
    evaluate_tradability,
)


def _scope(
    *,
    account_id: str = "DU123",
    strategy_id: str = "strategy_a",
    strategy_instance_id: str = "strategy_a_001",
    symbol: str = "NVDA",
    flow_type: FlowType = FlowType.ENTRY,
    session: str = "RTH",
    asset_type: str = "STK",
) -> TradabilityScope:
    return TradabilityScope(
        account_id=account_id,
        strategy_id=strategy_id,
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        flow_type=flow_type,
        session=session,
        asset_type=asset_type,
        order_role=flow_type.value,
    )


def test_entry_allowed_when_no_blockers_and_session_allowed() -> None:
    decision = evaluate_tradability(_scope())

    assert decision.allow is True
    assert decision.halt_class == HaltClass.NONE
    assert decision.reason == "allowed"


def test_entry_denied_by_matching_strategy_instance_lock() -> None:
    decision = evaluate_tradability(
        _scope(),
        TradabilityInputs(
            scoped_blockers=(
                ScopedBlocker(
                    blocker="strategy_manual_lock",
                    scope_type=BlockerScopeType.STRATEGY_INSTANCE,
                    account_id="DU123",
                    strategy_instance_id="strategy_a_001",
                ),
            )
        ),
    )

    assert decision.allow is False
    assert decision.halt_class == HaltClass.SCOPED_BLOCK
    assert decision.blockers == ("strategy_manual_lock",)


def test_entry_not_denied_by_other_strategy_instance_lock_same_symbol() -> None:
    decision = evaluate_tradability(
        _scope(strategy_instance_id="strategy_b_001"),
        TradabilityInputs(
            scoped_blockers=(
                ScopedBlocker(
                    blocker="strategy_manual_lock",
                    scope_type=BlockerScopeType.STRATEGY_INSTANCE,
                    account_id="DU123",
                    strategy_instance_id="strategy_a_001",
                    symbol="NVDA",
                ),
            )
        ),
    )

    assert decision.allow is True


def test_entry_denied_by_account_symbol_lock() -> None:
    decision = evaluate_tradability(
        _scope(),
        TradabilityInputs(
            scoped_blockers=(
                ScopedBlocker(
                    blocker="symbol_lock",
                    scope_type=BlockerScopeType.ACCOUNT_SYMBOL,
                    account_id="DU123",
                    symbol="NVDA",
                ),
            )
        ),
    )

    assert decision.allow is False
    assert decision.blockers == ("symbol_lock",)


def test_close_only_allowed_when_entry_blocked_by_session() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.EXIT, session="POST"),
        TradabilityInputs(session_policy=SessionPolicy(SessionPolicyType.RTH_ONLY_EQUITY)),
    )

    assert decision.allow is True
    assert decision.close_only_allowed is True
    assert decision.reduce_only_allowed is False
    assert decision.blockers == ("session_policy_rth_only_entry_blocked",)
    assert decision.overrides == ("flow_override:EXIT:entry_blocked",)


def test_stop_loss_allowed_when_entry_blocked_by_eod_with_owner_identity() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.STOP_LOSS, session="POST"),
        TradabilityInputs(
            scoped_blockers=(
                ScopedBlocker(
                    blocker="eod_rth_only_entry_blocked",
                    scope_type=BlockerScopeType.SESSION,
                    strategy_id="strategy_a",
                    session="POST",
                ),
            ),
            owner_identity_complete=True,
        ),
    )

    assert decision.allow is True
    assert decision.overrides == ("flow_override:STOP_LOSS:entry_blocked",)
    assert decision.reduce_only_allowed is True


def test_stop_loss_denied_without_owner_identity() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.STOP_LOSS),
        TradabilityInputs(
            scoped_blockers=(
                ScopedBlocker(
                    blocker="symbol_lock",
                    scope_type=BlockerScopeType.ACCOUNT_SYMBOL,
                    account_id="DU123",
                    symbol="NVDA",
                ),
            ),
            owner_identity_complete=False,
        ),
    )

    assert decision.allow is False
    assert decision.reason == "owner_identity_incomplete"
    assert decision.halt_class == HaltClass.SCOPED_BLOCK


def test_account_emergency_flatten_requires_emergency_flag() -> None:
    blocked = evaluate_tradability(_scope(flow_type=FlowType.ACCOUNT_EMERGENCY_FLATTEN))
    allowed = evaluate_tradability(
        _scope(flow_type=FlowType.ACCOUNT_EMERGENCY_FLATTEN),
        TradabilityInputs(emergency_authorized=True),
    )

    assert blocked.allow is False
    assert blocked.reason == "account_emergency_flatten_not_authorized"
    assert allowed.allow is True
    assert allowed.overrides == ("account_emergency_authorized",)


def test_account_emergency_flatten_cannot_bypass_safety_kernel_global_halt() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.ACCOUNT_EMERGENCY_FLATTEN),
        TradabilityInputs(
            global_blockers=("safety_kernel_global_halt",),
            emergency_authorized=True,
        ),
    )

    assert decision.allow is False
    assert decision.halt_class == HaltClass.GLOBAL_HALT
    assert decision.blockers == ("safety_kernel_global_halt",)


def test_reconciliation_report_allowed_without_entry_permission() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.RECONCILIATION_REPORT),
        TradabilityInputs(
            global_blockers=("broker_truth_missing",),
            scoped_blockers=(
                ScopedBlocker(
                    blocker="strategy_manual_lock",
                    scope_type=BlockerScopeType.STRATEGY_INSTANCE,
                    account_id="DU123",
                    strategy_instance_id="strategy_a_001",
                ),
            ),
        ),
    )

    assert decision.allow is True
    assert decision.reason == "report_only_allowed"
    assert "broker_truth_missing" in decision.warnings
    assert "strategy_manual_lock" in decision.warnings


def test_decision_reason_and_authority_are_populated() -> None:
    decision = evaluate_tradability(
        _scope(session="OVERNIGHT"),
        TradabilityInputs(session_policy=SessionPolicy(SessionPolicyType.RTH_ONLY_EQUITY)),
    )

    assert decision.authority == "TradabilityService"
    assert decision.reason
    assert decision.generated_at
