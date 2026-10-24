from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.order_manager import run_order_manager
from scripts.ai_trader.research.account_sync import sync_account_snapshot
from scripts.ai_trader.research.position_sync import sync_positions_snapshot
from scripts.ai_trader.research.live_risk_enforcer import enforce_live_risk
from scripts.ai_trader.research.execution_reconcile import run_execution_reconcile
from scripts.ai_trader.research.live_kill_switch import run_live_kill_switch
from scripts.ai_trader.research.recovery_playbook import run_recovery_playbook


def _write_live_manifest(root: Path, cid: str, symbols: list[str]) -> None:
    d = root / cid
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "candidate_id": cid,
        "experiment_id": f"exp-{cid}",
        "symbols": symbols,
        "target_weight": 0.5,
        "max_capital": 20_000,
        "risk_limits": {"risk_budget_bps": 10},
    }
    (d / "live_manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_live_broker_pipeline_outputs(tmp_path: Path) -> None:
    live_dir = tmp_path / "live_candidates"
    out = tmp_path / "out"
    _write_live_manifest(live_dir, "cand-a", ["SPY", "QQQ"])
    _write_live_manifest(live_dir, "cand-b", ["IWM"])
    risk_guard = {
        "items": [
            {"candidate_id": "cand-a", "allowed": True, "reasons": []},
            {"candidate_id": "cand-b", "allowed": False, "reasons": ["blocked_candidate"]},
        ]
    }

    oms = run_order_manager(
        cycle_id="fixture-live-broker-2026-04-05",
        live_candidates_dir=live_dir,
        risk_guard_report=risk_guard,
        output_dir=out,
        broker_profile="paper",
    )
    items = oms["orders_state"]["items"]
    statuses = {x["status"] for x in items}
    assert "filled" in statuses
    assert "partially_filled" in statuses
    assert "rejected" in statuses
    assert Path(oms["broker_session_json"]).exists()
    assert Path(oms["broker_profile_report_json"]).exists()
    assert Path(oms["broker_binding_report_json"]).exists()
    assert Path(oms["real_broker_dry_run_json"]).exists()
    assert Path(oms["broker_launch_switch_json"]).exists()
    assert Path(oms["orders_state_json"]).exists()
    assert Path(oms["orders_events_jsonl"]).exists()

    account = sync_account_snapshot(
        cycle_id="fixture-live-broker-2026-04-05",
        broker_snapshot=oms["broker_snapshot"],
        output_dir=out,
    )
    pos = sync_positions_snapshot(
        cycle_id="fixture-live-broker-2026-04-05",
        broker_snapshot=oms["broker_snapshot"],
        live_candidates_dir=live_dir,
        output_dir=out,
    )
    live_risk = enforce_live_risk(
        cycle_id="fixture-live-broker-2026-04-05",
        orders_state=oms["orders_state"],
        positions_snapshot=pos["positions_snapshot"],
        output_dir=out,
    )
    reconcile = run_execution_reconcile(
        cycle_id="fixture-live-broker-2026-04-05",
        orders_state=oms["orders_state"],
        broker_snapshot=oms["broker_snapshot"],
        position_reconcile_report=pos["position_reconcile_report"],
        output_dir=out,
    )
    kill = run_live_kill_switch(
        cycle_id="fixture-live-broker-2026-04-05",
        operator_controls={"global_pause": True, "blocked_candidates": ["cand-a"], "blocked_symbols": ["QQQ"]},
        live_risk_report=live_risk["live_risk_report"],
        positions_snapshot=pos["positions_snapshot"],
        output_dir=out,
    )
    recovery = run_recovery_playbook(
        cycle_id="fixture-live-broker-2026-04-05",
        broker_session=json.loads(Path(oms["broker_session_json"]).read_text(encoding="utf-8")),
        account_snapshot=account["account_snapshot"],
        positions_snapshot=pos["positions_snapshot"],
        orders_state=oms["orders_state"],
        kill_switch_report=kill["kill_switch_report"],
        output_dir=out,
    )

    assert Path(account["account_snapshot_json"]).exists()
    assert Path(pos["positions_snapshot_json"]).exists()
    assert Path(pos["position_reconcile_report_json"]).exists()
    assert Path(live_risk["live_risk_report_json"]).exists()
    assert Path(reconcile["execution_reconcile_report_json"]).exists()
    assert Path(kill["kill_switch_report_json"]).exists()
    assert Path(recovery["recovery_report_json"]).exists()
    assert live_risk["live_risk_report"]["summary"]["rejected_orders"] >= 1
    assert live_risk["live_risk_report"]["summary"]["flatten_actions"] >= 1
    assert reconcile["execution_reconcile_report"]["summary"]["extra_fill"] >= 1
    assert kill["kill_switch_report"]["kill_switch_active"] is True
    assert recovery["recovery_report"]["recovered"] is True


def test_real_broker_dry_run_controlled_fail_without_credentials(tmp_path: Path) -> None:
    live_dir = tmp_path / "live_candidates"
    out = tmp_path / "out"
    state_root = tmp_path / "state"
    _write_live_manifest(live_dir, "cand-real", ["SPY"])
    (state_root).mkdir(parents=True, exist_ok=True)
    (state_root / "operator_controls.json").write_text(
        json.dumps(
            {
                "global_pause": False,
                "paper_only": False,
                "live_enabled": True,
                "max_live_capital": 100000.0,
                "blocked_candidates": [],
                "blocked_symbols": [],
                "force_flatten": False,
            }
        ),
        encoding="utf-8",
    )
    (state_root / "broker_launch_switch.json").write_text(
        json.dumps(
            {
                "paper_only": False,
                "real_broker_enabled": True,
                "real_broker_dry_run_only": True,
            }
        ),
        encoding="utf-8",
    )
    risk_guard = {"items": [{"candidate_id": "cand-real", "allowed": True, "reasons": []}]}
    oms = run_order_manager(
        cycle_id="fixture-real-dry-run-2026-04-05",
        live_candidates_dir=live_dir,
        risk_guard_report=risk_guard,
        output_dir=out,
        broker_profile="real",
        operator_state_root=state_root,
    )
    dry = json.loads(Path(oms["real_broker_dry_run_json"]).read_text(encoding="utf-8"))
    profile = json.loads(Path(oms["broker_profile_report_json"]).read_text(encoding="utf-8"))
    assert profile["broker_profile"] == "real"
    assert profile["safe_default"] is True
    assert dry["status"] in {"controlled_fail", "success"}
    assert "account_probe" in dry
    assert "preflight_order_validation" in dry
