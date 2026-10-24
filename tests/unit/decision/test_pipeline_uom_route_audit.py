from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from libs.strategy.base import DecisionEnvelope, Freshness
from services.decision.pipeline import DecisionPipelineService
from services.exec.execution_service import ExecutionResult


class _MS:
    async def get(self, symbol):
        return {
            "halted": False,
            "bid_px": 99.9,
            "ask_px": 100.1,
            "mid_px": 100.0,
            "last_px": 100.0,
            "tick_size": 0.01,
        }


class _Bars:
    async def window(self, symbol, end_us, count):
        return []


class _Ledger:
    async def snapshot(self):
        return {"items": {}}


class _AuditWriter:
    def __init__(self):
        self.rows = []

    def emit(self, **kwargs):
        self.rows.append(kwargs)


def _pipeline(*, audit_writer, dry_run_mode=False):
    return DecisionPipelineService(
        market_state_provider=_MS(),
        bar_store=_Bars(),
        feature_store=AsyncMock(),
        reserve_ledger=_Ledger(),
        adv_provider=None,
        tick_size_provider=None,
        dry_run_mode=dry_run_mode,
        bypass_validator=True,
        bypass_arbiter=True,
        audit_writer=audit_writer,
    )


def _env(**extras):
    base_extras = {
        "account_id": "PAPER",
        "strategy_instance_id": "s1-inst",
        "intent_id": "intent-1",
        "client_order_id": "client-1",
        "trace_id": "trace-1",
        "ref_px": 100.0,
        "edge_bp": 10.0,
        "uom_intent_kind": "entry",
    }
    base_extras.update(extras)
    return DecisionEnvelope(
        decision_id="decision-1",
        strategy_id="s1",
        symbol="SPY",
        ts_bar_end_us=1,
        target_qty=1.0,
        freshness=Freshness(ttl_ms=1000, dlb_ms=30000),
        max_notional=100000.0,
        extras=base_extras,
    )


@pytest.mark.asyncio
async def test_uom_route_event_emitted_after_submit_intent():
    audit = _AuditWriter()
    pipe = _pipeline(audit_writer=audit)

    class _Delegate:
        def __init__(self):
            self.submit_entry = AsyncMock(
                return_value=ExecutionResult(success=True, decision_id="decision-1", reason="ok")
            )

    pipe.set_execution_service(_Delegate())

    await pipe.process_decision(_env())

    route = next(row for row in audit.rows if row["stage"] == "uom_delegated")
    assert route["payload"]["uom_route_status"] == "delegated"
    assert route["payload"]["uom_delegate_target"] == "submit_entry"


@pytest.mark.asyncio
async def test_uom_route_preserves_trace_decision_intent_owner_ids():
    audit = _AuditWriter()
    pipe = _pipeline(audit_writer=audit)

    class _Delegate:
        def __init__(self):
            self.submit_entry = AsyncMock(
                return_value=ExecutionResult(success=True, decision_id="decision-1", reason="ok")
            )

    pipe.set_execution_service(_Delegate())

    await pipe.process_decision(_env())

    payload = next(row for row in audit.rows if row["stage"] == "uom_delegated")["payload"]
    assert payload["trace_id"] == "trace-1"
    assert payload["decision_id"] == "decision-1"
    assert payload["intent_id"] == "intent-1"
    assert payload["client_order_id"] == "client-1"
    assert payload["strategy_instance_id"] == "s1-inst"
    assert payload["owner_identity_complete"] is True


@pytest.mark.asyncio
async def test_uom_missing_delegate_records_reject_reason():
    audit = _AuditWriter()
    pipe = _pipeline(audit_writer=audit, dry_run_mode=False)

    await pipe.process_decision(_env())

    route = next(row for row in audit.rows if row["stage"] == "uom_rejected")
    assert route["payload"]["uom_route_status"] == "rejected"
    assert route["payload"]["uom_reject_reason"] == "no_delegate_registered"


@pytest.mark.asyncio
async def test_uom_shadow_mode_records_skipped_not_unknown_loss():
    audit = _AuditWriter()
    pipe = _pipeline(audit_writer=audit, dry_run_mode=True)

    await pipe.process_decision(_env())

    route = next(row for row in audit.rows if row["stage"] == "uom_skipped_shadow")
    assert route["payload"]["uom_route_status"] == "skipped"
    assert route["payload"]["uom_reject_reason"] == "execution_mode_shadow_no_delegate"
    assert route["payload"]["uom_shadow_mode"] is True
