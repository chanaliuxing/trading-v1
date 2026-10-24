from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.broker_models import BrokerSession
from scripts.ai_trader.research.paper_broker_adapter import PaperBrokerAdapter
from scripts.ai_trader.research.real_broker_adapter import RealBrokerDryRunAdapter
from scripts.ai_trader.research.broker_readonly_probe import run_broker_readonly_probe
from scripts.ai_trader.research.broker_sandbox_orders import run_broker_sandbox_orders
from scripts.ai_trader.research.broker_reconcile import run_broker_reconcile
from scripts.ai_trader.research.eod_closeout import run_eod_closeout
from scripts.ai_trader.research.broker_recovery import run_broker_recovery
from scripts.ai_trader.research.broker_launch_barrier import apply_broker_launch_barrier


def test_broker_readonly_probe_controlled_fail_and_success(tmp_path: Path) -> None:
    real_fail = RealBrokerDryRunAdapter(
        session_id="s1",
        broker_profile="real",
        credential_source="missing",
        credential_valid=False,
        dry_run_only=True,
    )
    fail = run_broker_readonly_probe(
        cycle_id="c-ro-fail",
        broker=real_fail,
        broker_profile="real",
        output_dir=tmp_path,
    )["broker_readonly_probe"]
    assert fail["status"] == "controlled_fail"
    assert "account_probe" in fail

    real_ok = RealBrokerDryRunAdapter(
        session_id="s2",
        broker_profile="real",
        credential_source="env",
        credential_valid=True,
        dry_run_only=True,
    )
    ok = run_broker_readonly_probe(
        cycle_id="c-ro-ok",
        broker=real_ok,
        broker_profile="real",
        output_dir=tmp_path,
    )["broker_readonly_probe"]
    assert ok["status"] == "success"
    assert ok["account_probe"]["ok"] is True


def test_sandbox_orders_reconcile_eod_recovery_barrier(tmp_path: Path) -> None:
    sandbox = run_broker_sandbox_orders(
        cycle_id="fixture-sandbox-2026-04-05",
        broker_profile_report={
            "broker_profile": "real",
            "launch_switch": {"real_broker_dry_run_only": True},
        },
        broker_safety_gate={"gate_status": "approved"},
        operator_controls={"live_enabled": True, "global_pause": False},
        risk_guard_report={"items": []},
        capital_governor_report={"items": []},
        session_control_report={"items": []},
        output_dir=tmp_path,
    )
    orders_payload = sandbox["broker_sandbox_orders"]
    assert orders_payload["summary"]["submitted_count"] >= 1
    assert orders_payload["summary"]["canceled_count"] >= 1
    assert orders_payload["summary"]["rejected_count"] >= 1
    assert Path(sandbox["broker_sandbox_events_jsonl"]).exists()

    # Build mismatch and clean reconcile samples.
    broker = PaperBrokerAdapter(session_id=BrokerSession("s3", "paper", []).session_id, broker_profile="paper")
    o1 = broker.submit_order(
        client_order_id="o1",
        symbol="SPY",
        side="BUY",
        qty=2,
        price=100.0,
        candidate_id="c1",
        experiment_id="e1",
        behavior="partial",
    )
    snapshot = broker.export_state()
    reconcile_mismatch = run_broker_reconcile(
        cycle_id="fixture-sandbox-2026-04-05",
        orders_state={"items": [{"client_order_id": "o1", "broker_order_id": o1["broker_order_id"], "status": "accepted", "symbol": "SPY"}]},
        broker_snapshot=snapshot,
        positions_snapshot={"positions": [{"symbol": "QQQ", "qty": 1, "avg_price": 100.0}]},
        output_dir=tmp_path,
    )["broker_reconcile_report"]
    assert reconcile_mismatch["summary"]["status_mismatch"] >= 1
    assert reconcile_mismatch["summary"]["position_mismatch"] >= 1

    reconcile_clean = run_broker_reconcile(
        cycle_id="c-clean",
        orders_state={"items": []},
        broker_snapshot={"orders": [], "fills": []},
        positions_snapshot={"positions": []},
        output_dir=tmp_path,
    )["broker_reconcile_report"]
    assert reconcile_clean["clean_reconcile"] is True

    eod = run_eod_closeout(
        cycle_id="c-eod",
        broker_snapshot={
            "orders": [{"broker_order_id": "b1", "client_order_id": "o1", "status": "accepted"}],
            "positions": [{"symbol": "SPY", "qty": 1, "avg_price": 100.0}],
            "account": {"cash": 100000},
        },
        kill_switch_report={"kill_switch_active": True},
        output_dir=tmp_path,
    )["eod_closeout_report"]
    assert eod["pending_orders_count"] >= 1
    assert eod["flatten_required"] is True

    recovery = run_broker_recovery(
        cycle_id="c-recover",
        broker=RealBrokerDryRunAdapter(
            session_id="s4",
            broker_profile="real",
            credential_source="env",
            credential_valid=True,
            dry_run_only=True,
        ),
        broker_session={"session_id": "s4"},
        output_dir=tmp_path,
    )["broker_recovery_report"]
    assert recovery["recovery_success_sample"]["status"] in {"success", "controlled_fail"}
    assert recovery["recovery_controlled_fail_sample"]["status"] == "controlled_fail"

    barrier = apply_broker_launch_barrier(
        cycle_id="c-barrier",
        broker_readonly_probe={"status": "controlled_fail"},
        broker_reconcile_report={"summary": {"missing_order": 1, "status_mismatch": 0, "fill_mismatch": 0, "position_mismatch": 0}},
        eod_closeout_report={"closeout_completed": False},
        launch_registry={"last_verdict": "go_small_live"},
        active_registry={"items": [{"candidate_id": "c1", "status": "active"}]},
        system_status={"live_enabled": True, "kill_switch_active": False},
        state_root=tmp_path / "state",
        output_dir=tmp_path,
    )["broker_launch_barrier"]
    assert barrier["barrier_status"] == "blocked"
    assert "readonly_probe_not_success" in barrier["block_reasons"]

    # Ensure artifact files exist.
    for name in [
        "broker_sandbox_orders.json",
        "broker_sandbox_events.jsonl",
        "broker_reconcile_report.json",
        "eod_closeout_report.json",
        "broker_recovery_report.json",
        "broker_launch_barrier.json",
    ]:
        assert (tmp_path / name).exists()

