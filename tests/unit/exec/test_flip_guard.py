from unittest.mock import AsyncMock, MagicMock
import time

import pytest

from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.exec.flip_guard import FlipGuardRegistry


class _ContractDetails:
    def __init__(self, underSymbol):
        self.underSymbol = underSymbol


@pytest.mark.asyncio
async def test_flip_guard_blocks_reopen_within_cooldown():
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        flip_guard_cooldown_sec=60.0,
    )
    svc._update_order_rejected = AsyncMock(return_value=None)
    svc._emit_event = AsyncMock(return_value=None)
    svc.risk.release = AsyncMock(return_value=None)
    svc.flip_guard.record_submission(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="SELL",
        extras={"flatten_action": True, "close_only": True, "bar_id": "m1:100"},
        now_epoch_s=6_000.0,
    )
    plan = ExecPlan(
        order_type="MKT",
        side="BUY",
        qty=10.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=3.0,
        oco={},
        child={},
        notes={},
    )

    result = await svc._enforce_flip_guard(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=plan,
        decision_id="d-1",
        idempotency_key="idem-1",
        envelope_extras={"bar_id": "m1:100"},
        trace_ctx=None,
    )

    assert result is not None
    assert result.success is False
    assert result.reason == "same_bar_reversal_block"


def test_flip_guard_blocks_same_bar_side_flip():
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
    )
    svc.flip_guard.record_submission(
        strategy_id="s1",
        symbol="TQQQ",
        side="BUY",
        extras={"bar_id": "m1:200"},
        now_epoch_s=12_000.0,
    )

    verdict = svc.flip_guard.check_open_allowed(
        strategy_id="s2",
        symbol="SQQQ",
        side="SELL",
        extras={"bar_id": "m1:200"},
        now_epoch_s=12_001.0,
    )

    assert verdict.allowed is False
    assert verdict.reason == "same_bar_side_flip"


def test_flip_guard_persists_state_across_instances(tmp_path):
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    state_file = tmp_path / "flip_guard.json"
    svc1 = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
    )
    svc1.flip_guard._state_path = state_file
    svc1.flip_guard.record_submission(
        strategy_id="s1",
        symbol="TQQQ",
        side="SELL",
        extras={"flatten_action": True, "close_only": True, "bar_id": "m1:300"},
        now_epoch_s=18_000.0,
    )

    svc2 = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
    )
    svc2.flip_guard._state_path = state_file
    svc2.flip_guard._load_state()
    verdict = svc2.flip_guard.check_open_allowed(
        strategy_id="s2",
        symbol="TQQQ",
        side="BUY",
        extras={"bar_id": "m1:300"},
        now_epoch_s=18_001.0,
    )

    assert verdict.allowed is False
    assert verdict.reason == "same_bar_reversal_block"


def test_flip_guard_exemption_requires_future_expiry():
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        flip_guard_exempt_strategy_ids=["legacy_without_expiry"],
    )
    svc.flip_guard = FlipGuardRegistry(exempt_strategy_ids=["legacy_without_expiry"], state_path=None)

    verdict = svc.flip_guard.check_open_allowed(
        strategy_id="legacy_without_expiry",
        symbol="TQQQ",
        side="BUY",
        extras={"bar_id": "m1:1"},
        now_epoch_s=1_000.0,
    )

    assert verdict.allowed is True
    assert verdict.reason == "allowed"


def test_flip_guard_allows_timeboxed_exemption():
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=True,
        flip_guard_exemptions=[
            {"strategy_id": "temporary_exempt", "expires_at": time.time() + 3600, "reason": "fault_injection"}
        ],
    )
    svc.flip_guard = FlipGuardRegistry(
        exempt_strategy_ids=[
            {"strategy_id": "temporary_exempt", "expires_at": time.time() + 3600, "reason": "fault_injection"}
        ],
        state_path=None,
    )

    verdict = svc.flip_guard.check_open_allowed(
        strategy_id="temporary_exempt",
        symbol="TQQQ",
        side="BUY",
        extras={"bar_id": "m1:1"},
    )

    assert verdict.allowed is True
    assert verdict.reason == "exempt_strategy"
    assert verdict.metadata["reason"] == "fault_injection"


def test_flip_guard_resolves_root_from_contract_metadata_dict():
    guard = FlipGuardRegistry(state_path=None)

    assert (
        guard.resolve_root_symbol(
            "SQQQ",
            extras={"contract_details": {"underSymbol": "QQQ"}},
        )
        == "QQQ"
    )


def test_flip_guard_resolves_root_from_contract_metadata_object():
    guard = FlipGuardRegistry(state_path=None)

    assert (
        guard.resolve_root_symbol(
            "SOXS",
            extras={"contract_details": _ContractDetails("SOX")},
        )
        == "SOX"
    )


def test_flip_guard_normalizes_option_risk_symbol_to_underlying_root():
    guard = FlipGuardRegistry(state_path=None)

    assert guard.resolve_root_symbol("IGNORED", extras={"risk_symbol": "OPT:QQQ:1DTE"}) == "QQQ"


@pytest.mark.asyncio
async def test_flip_guard_hydrates_and_persists_pg_state():
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        return_value=[
            {
                "root_symbol": "QQQ",
                "action": "flat",
                "symbol": "TQQQ",
                "side": "SELL",
                "strategy_id": "s1",
                "bar_id": "m1:500",
                "ts_epoch_s": 30_000.0,
                "metadata": {"raw_action": "flatten"},
            }
        ]
    )
    pg.execute = AsyncMock(return_value=None)
    guard = FlipGuardRegistry(state_path=None)

    await guard.hydrate_from_pg(pg)
    verdict = guard.check_open_allowed(
        strategy_id="s2",
        symbol="SQQQ",
        side="BUY",
        extras={"bar_id": "m1:500"},
        now_epoch_s=30_001.0,
    )

    assert verdict.allowed is False
    assert verdict.reason == "same_bar_reversal_block"

    record = guard.record_submission(
        strategy_id="s2",
        symbol="SQQQ",
        side="BUY",
        extras={"bar_id": "m1:501"},
        now_epoch_s=30_060.0,
    )
    await guard.persist_submission_to_pg(pg, record)

    insert_call = next(call for call in pg.execute.await_args_list if "INSERT INTO core.core_flip_guard" in call.args[0])
    assert insert_call.args[1] == "QQQ"
    assert insert_call.args[2] == "entry"
