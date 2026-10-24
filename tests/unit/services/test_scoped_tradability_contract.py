from __future__ import annotations

import pytest

from tool.ops.run_runtime_operations_watchdog import _build_production_state
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
    asset_type: str = "STK",
    session: str = "RTH",
    flow_type: FlowType = FlowType.ENTRY,
) -> TradabilityScope:
    return TradabilityScope(
        account_id=account_id,
        strategy_id=strategy_id,
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        asset_type=asset_type,
        session=session,
        flow_type=flow_type,
        order_role=flow_type.value,
    )


def _strategy_lock(strategy_instance_id: str = "strategy_a_001") -> ScopedBlocker:
    return ScopedBlocker(
        blocker="strategy_manual_lock",
        scope_type=BlockerScopeType.STRATEGY_INSTANCE,
        account_id="DU123",
        strategy_instance_id=strategy_instance_id,
    )


def _symbol_lock(symbol: str = "NVDA") -> ScopedBlocker:
    return ScopedBlocker(
        blocker="symbol_lock",
        scope_type=BlockerScopeType.ACCOUNT_SYMBOL,
        account_id="DU123",
        symbol=symbol,
    )


def test_global_halt_blocks_entry_for_all_scopes() -> None:
    for scope in (_scope(symbol="NVDA"), _scope(strategy_instance_id="strategy_b_001", symbol="MSFT")):
        decision = evaluate_tradability(scope, TradabilityInputs(global_blockers=("safety_kernel_global_halt",)))

        assert decision.allow is False
        assert decision.halt_class == HaltClass.GLOBAL_HALT
        assert decision.blockers == ("safety_kernel_global_halt",)


def test_broker_truth_missing_is_global_halt() -> None:
    decision = evaluate_tradability(_scope(), TradabilityInputs(global_blockers=("broker_truth_missing",)))

    assert decision.allow is False
    assert decision.halt_class == HaltClass.GLOBAL_HALT
    assert decision.reason == "broker_truth_missing"


def test_position_safety_no_go_is_global_halt() -> None:
    decision = evaluate_tradability(_scope(), TradabilityInputs(global_blockers=("position_safety_no_go",)))

    assert decision.allow is False
    assert decision.halt_class == HaltClass.GLOBAL_HALT
    assert decision.blockers == ("position_safety_no_go",)


def test_runtime_stale_is_global_halt() -> None:
    decision = evaluate_tradability(_scope(), TradabilityInputs(global_blockers=("runtime_version_loaded_code_stale",)))

    assert decision.allow is False
    assert decision.halt_class == HaltClass.GLOBAL_HALT


def test_manual_recovery_required_is_global_halt() -> None:
    decision = evaluate_tradability(_scope(), TradabilityInputs(global_blockers=("manual_recovery_required",)))

    assert decision.allow is False
    assert decision.halt_class == HaltClass.GLOBAL_HALT


def test_strategy_manual_lock_blocks_only_matching_strategy_instance() -> None:
    locked = evaluate_tradability(_scope(), TradabilityInputs(scoped_blockers=(_strategy_lock(),)))
    other = evaluate_tradability(
        _scope(strategy_instance_id="strategy_b_001"),
        TradabilityInputs(scoped_blockers=(_strategy_lock(),)),
    )

    assert locked.allow is False
    assert locked.halt_class == HaltClass.SCOPED_BLOCK
    assert locked.blockers == ("strategy_manual_lock",)
    assert other.allow is True
    assert other.halt_class == HaltClass.NONE


def test_symbol_lock_blocks_only_matching_account_symbol_scope() -> None:
    locked = evaluate_tradability(_scope(symbol="NVDA"), TradabilityInputs(scoped_blockers=(_symbol_lock("NVDA"),)))
    other_symbol = evaluate_tradability(_scope(symbol="MSFT"), TradabilityInputs(scoped_blockers=(_symbol_lock("NVDA"),)))
    other_account = evaluate_tradability(
        _scope(account_id="DU999", symbol="NVDA"),
        TradabilityInputs(scoped_blockers=(_symbol_lock("NVDA"),)),
    )

    assert locked.allow is False
    assert locked.blockers == ("symbol_lock",)
    assert other_symbol.allow is True
    assert other_account.allow is True


def test_eod_rth_only_blocks_entry_after_rth_close() -> None:
    decision = evaluate_tradability(
        _scope(session="POST"),
        TradabilityInputs(session_policy=SessionPolicy(SessionPolicyType.RTH_ONLY_EQUITY)),
    )

    assert decision.allow is False
    assert decision.halt_class == HaltClass.SCOPED_BLOCK
    assert decision.blockers == ("session_policy_rth_only_entry_blocked",)


def test_eod_extended_strategy_allows_post_market_entry() -> None:
    decision = evaluate_tradability(
        _scope(session="POST"),
        TradabilityInputs(
            session_policy=SessionPolicy(
                SessionPolicyType.EXTENDED_EQUITY,
                allowed_sessions=("RTH", "PRE", "POST", "OVERNIGHT"),
            )
        ),
    )

    assert decision.allow is True
    assert decision.halt_class == HaltClass.NONE


def test_options_default_rth_only_unless_policy_allows_extended() -> None:
    blocked = evaluate_tradability(
        _scope(asset_type="OPT", session="POST"),
        TradabilityInputs(session_policy=SessionPolicy(SessionPolicyType.OPTIONS_RTH_ONLY_DEFAULT)),
    )
    allowed = evaluate_tradability(
        _scope(asset_type="OPT", session="POST"),
        TradabilityInputs(
            session_policy=SessionPolicy(
                SessionPolicyType.OPTIONS_RTH_ONLY_DEFAULT,
                allowed_sessions=("RTH", "POST"),
                allow_extended_options=True,
            )
        ),
    )

    assert blocked.allow is False
    assert blocked.blockers == ("session_policy_options_rth_only_entry_blocked",)
    assert allowed.allow is True


def test_protective_stop_allowed_when_entry_blocked_by_session() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.STOP_LOSS, session="POST"),
        TradabilityInputs(session_policy=SessionPolicy(SessionPolicyType.RTH_ONLY_EQUITY)),
    )

    assert decision.allow is True
    assert decision.close_only_allowed is True
    assert decision.reduce_only_allowed is True
    assert decision.blockers == ("session_policy_rth_only_entry_blocked",)
    assert decision.overrides == ("flow_override:STOP_LOSS:entry_blocked",)


def test_strategy_flatten_allowed_when_entry_blocked_by_eod() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.STRATEGY_FLATTEN, session="POST"),
        TradabilityInputs(
            scoped_blockers=(
                ScopedBlocker(
                    blocker="eod_rth_only_entry_blocked",
                    scope_type=BlockerScopeType.SESSION,
                    strategy_id="strategy_a",
                    session="POST",
                ),
            )
        ),
    )

    assert decision.allow is True
    assert decision.close_only_allowed is True
    assert decision.reduce_only_allowed is True
    assert decision.overrides == ("flow_override:STRATEGY_FLATTEN:entry_blocked",)


def test_flow_override_cannot_bypass_broker_truth_missing() -> None:
    decision = evaluate_tradability(
        _scope(flow_type=FlowType.STOP_LOSS),
        TradabilityInputs(global_blockers=("broker_truth_missing",)),
    )

    assert decision.allow is False
    assert decision.halt_class == HaltClass.GLOBAL_HALT
    assert decision.overrides == ()


def test_strategy_system_paused_no_longer_only_hard_return_source() -> None:
    from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy

    assert hasattr(ComposableStocksKernelStrategy, "_entry_tradability_decision_while_paused")


@pytest.mark.asyncio
async def test_execution_service_requires_tradability_decision_before_entry() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from services.decision.pipeline import PipelineResult
    from services.decision.translator import ExecPlan
    from services.exec.execution_service import ExecutionService

    risk = MagicMock()
    risk.approve = AsyncMock()
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=risk,
        safety_kernel=MagicMock(),
        enable_execution=False,
        order_event_enabled=False,
    )
    result = PipelineResult(
        status="APPROVED",
        decision_id="d-tradability",
        strategy_id="strategy_a",
        symbol="NVDA",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=1,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={
            "envelope_extras": {
                "account_id": "DU123",
                "strategy_instance_id": "strategy_a_001",
                "intent_id": "intent-entry",
            },
            "scoped_tradability_inputs": {"global_blockers": ["broker_truth_missing"]},
        },
    )

    out = await svc.execute(result)

    assert out.success is False
    assert out.reason == "scoped_tradability_denied:broker_truth_missing"
    assert out.metadata["tradability_decision"]["authority"] == "TradabilityService"
    risk.approve.assert_not_awaited()


def test_production_state_can_express_partial_entry_go_no_go() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-12T10:00:00-04:00",
            "day": "20260512",
            "failures": [
                {
                    "issue_key": "strategy_tradability:strategy_a:session_closed",
                    "service": "STRATEGY_TRADABILITY",
                    "error_type": "strategy_tradability_no_go",
                    "detail": "session_closed",
                }
            ],
            "strategy_tradability": {
                "go_no_go": "no_go",
                "tradable_entry_ready_count": 1,
                "close_only_allowed_count": 1,
                "records": [
                    {
                        "strategy_id": "strategy_a",
                        "tradable_entry_ready": False,
                        "blockers": ["session_closed"],
                        "details": {
                            "account_id": "DU123",
                            "strategy_instance_id": "strategy_a_001",
                            "symbol": "NVDA",
                            "session": "POST",
                        },
                    },
                    {
                        "strategy_id": "strategy_b",
                        "tradable_entry_ready": True,
                        "blockers": [],
                        "details": {
                            "account_id": "DU123",
                            "strategy_instance_id": "strategy_b_001",
                            "symbol": "MSFT",
                            "session": "POST",
                        },
                    },
                ],
            },
        }
    )

    assert state["go_no_go"] == "go"
    assert state["global_halt"] is False
    assert state["entry_go_no_go"] == "partial"
    assert state["production_state_authorizes_execution"] is False
    assert state["blocked_scopes"][0]["strategy_instance_id"] == "strategy_a_001"
    assert state["blocked_scopes"][0]["blockers"] == ["session_closed"]
    assert state["flow_overrides"][0]["flow_type"] == "STRATEGY_FLATTEN"


def test_decision_evidence_contains_scope_flow_blockers_authority() -> None:
    scope = _scope(flow_type=FlowType.STOP_LOSS)
    decision = evaluate_tradability(
        scope,
        TradabilityInputs(scoped_blockers=(_symbol_lock("NVDA"),)),
        now=None,
    )

    assert decision.authority == "TradabilityService"
    assert decision.scope.account_id == "DU123"
    assert decision.scope.strategy_instance_id == "strategy_a_001"
    assert decision.scope.flow_type == FlowType.STOP_LOSS
    assert decision.scope.session == "RTH"
    assert decision.allow is True
    assert decision.blockers == ("symbol_lock",)
    assert decision.overrides == ("flow_override:STOP_LOSS:entry_blocked",)
    assert decision.generated_at
