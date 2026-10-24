"""
Regression tests for OMS strategy-isolation safety.

These tests intentionally encode the target safety contract for same-symbol
multi-strategy execution. Several are expected to fail until the OMS has
first-class strategy_instance/account/owner fields and owner-scoped broker
rebind/quarantine semantics.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.exec.models import OrderIntent
from services.exec.safety_kernel import Plan, SafetyKernel


ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _dataclass_field_names(cls: type) -> set[str]:
    return {f.name for f in fields(cls)}


class _FakePG:
    def __init__(self, *, pos_qty: float = 0.0) -> None:
        self.pos_qty = pos_qty

    async def fetchone(self, query: str, *args):
        if "FROM core.core_position" in query:
            return {"pos_qty": self.pos_qty}
        return None


def test_order_intent_requires_owner_fields():
    required = {
        "account_id",
        "strategy_id",
        "strategy_instance_id",
        "intent_id",
        "symbol",
        "order_role",
        "close_only",
        "reduce_only",
    }

    existing = _dataclass_field_names(OrderIntent)

    assert required <= existing, (
        "OrderIntent must carry immutable owner and role fields before live "
        f"same-symbol multi-strategy execution is safe; missing={sorted(required - existing)}"
    )


def test_quarantine_blocks_only_matching_strategy_instance():
    ddl = _read("db/pg/migration_20260417_ib_order_truth_and_quarantine.ddl")
    execution_service = _read("services/exec/execution_service.py")

    assert "strategy_instance_id" in ddl and "quarantine_scope" in ddl, (
        "core_order_quarantine must be owner-scoped; symbol-only quarantine blocks "
        "unrelated strategies on the same account+symbol."
    )
    assert "WHERE symbol = $1" not in execution_service, (
        "Execution quarantine gate must not default to symbol-only blocking."
    )


def test_safety_plan_key_separates_strategy_instance_and_lot():
    required_plan_fields = {"account_id", "strategy_instance_id", "symbol", "lot_id"}
    existing_plan_fields = _dataclass_field_names(Plan)

    assert required_plan_fields <= existing_plan_fields, (
        "SafetyKernel Plan must be owner/lot-scoped; missing="
        f"{sorted(required_plan_fields - existing_plan_fields)}"
    )

    kernel = SafetyKernel(broker_adapter=SimpleNamespace(), pg=None)
    key_a = kernel._plan_key("mean_revert", "SPY")
    key_b = kernel._plan_key("mean_revert", "SPY")
    assert key_a != key_b, (
        "Two strategy instances/lots for the same strategy_id+symbol must not "
        "collapse into one in-memory plan key."
    )


def test_strategy_a_cannot_cancel_strategy_b_order():
    source = _read("services/exec/execution_service.py")

    forbidden_fragments = [
        "return (strategy_id in r) and (sym_u in r)",
        "startswith(tag_prefix)",
        "startswith(option_entry_prefix)",
        "startswith(option_exit_prefix)",
    ]

    for fragment in forbidden_fragments:
        assert fragment not in source, (
            "Cancel ownership must be proven by an exact durable owner record, "
            f"not by order_ref prefix/substring heuristic: {fragment}"
        )


def test_strategy_a_stop_cannot_close_strategy_b_position():
    ledger_ddl = _read("db/pg/migration_20260421_uom_position_ledger.ddl")
    protection_ddl = ledger_ddl.split("CREATE TABLE IF NOT EXISTS core.position_ledger_event", 1)[0]

    assert "strategy_instance_id" in ledger_ddl, (
        "A stop-loss fill can only be proven isolated when position ledger rows "
        "include strategy_instance_id."
    )
    assert "bracket_id" in protection_ddl and "position_owner" in protection_ddl, (
        "Protective stops must bind to bracket_id/position_owner so Strategy A "
        "cannot close Strategy B's same-symbol position."
    )


def test_strategy_a_take_profit_cannot_close_strategy_b_position():
    safety_kernel = _read("services/exec/safety_kernel.py")

    assert 'f"lot:{plan.strategy_id}:{plan.symbol}"' not in safety_kernel, (
        "Default protection lot_id must not collapse all entries for a strategy+symbol."
    )
    assert "strategy_instance_id" in safety_kernel, (
        "Take-profit protection must be strategy-instance scoped."
    )


def test_opposite_direction_same_symbol_requires_explicit_policy():
    config = _read("config.yml")
    execution_service = _read("services/exec/execution_service.py")

    assert "same_symbol_multi_strategy_safe" in config, (
        "Same-symbol multi-strategy execution must require an explicit safety flag."
    )
    assert "virtual_hedge" in execution_service or "execution_arbiter" in execution_service, (
        "Opposite-direction same-symbol strategies require an explicit execution "
        "arbiter or virtual hedge policy."
    )


@pytest.mark.anyio
async def test_reduce_only_order_cannot_reverse_position():
    pg = _FakePG(pos_qty=100.0)
    service = ExecutionService(
        pg=pg,
        risk_service=SimpleNamespace(),
        safety_kernel=SimpleNamespace(broker=None),
        flatten_controller=SimpleNamespace(),
        flip_guard_enabled=False,
    )
    plan = ExecPlan(
        order_type="MKT",
        side="SELL",
        qty=150.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=0.0,
        oco={},
        child={"type": "NONE"},
        notes={},
    )

    ok, reason = await service._enforce_close_only_guard(
        strategy_id="STRAT_A",
        symbol="SPY",
        plan=plan,
        envelope_extras={"close_only": True, "reduce_only": True},
    )

    assert ok, reason
    assert plan.qty == 100.0


def test_reconnect_rebinds_open_orders_to_strategy_instance():
    core_order_ddl = _read("db/pg/pg_ddl.sql")
    ib_adapter = _read("services/exec/adapters/ib_adapter.py")

    assert "strategy_instance_id" in core_order_ddl, (
        "core_order must persist strategy_instance_id for reconnect rebind."
    )
    assert "UNKNOWN_OWNER" in ib_adapter, (
        "IBKR open orders without durable owner must be classified UNKNOWN_OWNER."
    )


def test_broker_aggregate_position_does_not_overwrite_virtual_positions():
    reconciler = _read("services/reconciliation/runtime_reconciler.py")
    position_safety = _read("services/production/position_safety_supervisor.py")

    assert "position_sync_enabled = False" in reconciler or "forced disabled" in reconciler
    assert "owner_lane_ledger_flat_broker_flat_no_open_orders" in position_safety, (
        "Position safety may clear a stale virtual-position cache only when exact owner-lane "
        "ledger evidence is flat and no owner-lane order remains open."
    )
    assert "broker-flat evidence is only a prerequisite" in position_safety, (
        "Broker aggregate position must remain insufficient on its own to clear or overwrite "
        "strategy virtual positions."
    )


def test_intraday_flat_only_closes_strategy_owned_position():
    manual_flatten = _read("services/telegram/manual_flatten_service.py")

    assert "FROM core.position_ledger_current" in manual_flatten, (
        "Intraday strategy flatten should target owner ledger rows, not legacy "
        "strategy_id+symbol core_position only."
    )
    assert "execute_flatten_all" not in manual_flatten, (
        "Account-wide flatten must not be the default path for strategy-level intraday flat."
    )


def test_overnight_position_detected_after_restart():
    supervisor = _read("services/production/position_safety_supervisor.py")

    assert "overnight_carry" in supervisor, (
        "Restart with residual broker position must create an overnight carry incident."
    )
    assert "candidate_strategy_ids" not in supervisor, (
        "Residual overnight positions must not be assigned by config-derived candidate strategies."
    )


def test_manual_broker_order_does_not_get_assigned_to_random_strategy():
    fill_ddl = _read("db/pg/migration_20260505_fill_attribution_ledger.ddl")
    convergence = _read("services/reconciliation/broker_truth_convergence.py")

    assert "manual_adjudication_required boolean DEFAULT true" in fill_ddl
    assert "matched_strategy_id" in fill_ddl
    assert "manual_required" in convergence or "UNKNOWN_OWNER" in convergence, (
        "Manual/unknown broker orders must require adjudication instead of symbol-based assignment."
    )


def test_orphan_order_does_not_trigger_duplicate_submit():
    reconciler = _read("services/reconciliation/runtime_reconciler.py")

    forbidden = ["place_market(", "place_limit(", "place_stop(", "submit_flatten("]
    for fragment in forbidden:
        assert fragment not in reconciler, (
            "Runtime reconciler must not submit duplicate orders while resolving DB-only orphans: "
            f"{fragment}"
        )


def test_stale_order_event_after_epoch_change_is_ignored_or_quarantined():
    event_ddl = _read("db/pg/migration_20260301_core_order_event.ddl")
    safety_kernel = _read("services/exec/safety_kernel.py")

    assert "source_event_id" in event_ddl and "ib_connection_epoch" in event_ddl, (
        "Order events need source_event_id + epoch to fence stale post-reconnect events."
    )
    assert "WHERE order_id = $2" not in safety_kernel, (
        "Order state updates must be monotonic and epoch-aware, not unconditional by order_id."
    )
