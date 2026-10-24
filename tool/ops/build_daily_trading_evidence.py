#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.market_calendar import is_trading_day
from tool.report.build_live_strategy_surface_report import (
    build_live_strategy_surface_report,
    evaluate_phase0_freeze_gate,
)
from tool.report.build_order_truth_runtime_evidence import build_order_truth_runtime_evidence
from tool.report.build_qqq_shadow_drift_report import build_qqq_shadow_drift_report
from tool.report.build_uom_caller_surface_report import (
    build_uom_caller_surface_report,
    evaluate_uom_caller_gate,
)
from tool.ops.runtime_reconciliation_health import build_runtime_reconciliation_health

ET = ZoneInfo("America/New_York")


def _repo_root() -> Path:
    return REPO_ROOT


def _today_yyyymmdd() -> str:
    return datetime.now(ET).strftime("%Y%m%d")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _text_contains(path: Path, needles: list[str]) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return all(needle in text for needle in needles)


def _build_protection_registry_evidence(repo_root: Path) -> dict[str, Any]:
    ddl_path = repo_root / "db/pg/migration_20260421_uom_position_ledger.ddl"
    manager_path = repo_root / "services/orders/manager.py"
    test_path = repo_root / "tests/unit/services/test_unified_order_manager.py"
    table_supported = _text_contains(ddl_path, ["core_protection_order_registry"])
    registry_write_supported = _text_contains(
        manager_path,
        [
            "_register_protection_order",
            "core.core_protection_order_registry",
            "_is_protection_intent",
        ],
    )
    stress_test_supported = _text_contains(
        test_path,
        [
            "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers",
            "take_profit",
        ],
    )
    ok = table_supported and registry_write_supported and stress_test_supported
    return {
        "ok": ok,
        "table_supported": table_supported,
        "registry_write_supported": registry_write_supported,
        "stress_test_supported": stress_test_supported,
        "ddl_path": str(ddl_path),
        "manager_path": str(manager_path),
        "stress_test_path": str(test_path),
    }


def _build_position_reconciler_evidence(repo_root: Path) -> dict[str, Any]:
    contracts_path = repo_root / "services/reconciliation/contracts.py"
    lot_level_supported = _text_contains(
        contracts_path,
        [
            "GROUP BY symbol, strategy_id, lot_id",
            "ledger_lots",
            "ledger_lot_count_by_symbol",
        ],
    )
    return {
        "ok": lot_level_supported,
        "lot_level_supported": lot_level_supported,
        "contracts_path": str(contracts_path),
    }


def _build_quarantine_evidence(repo_root: Path) -> dict[str, Any]:
    fill_handler_path = repo_root / "services/exec/fill_event_handler.py"
    quarantine_handler_path = repo_root / "services/reconciliation/order_quarantine.py"
    runtime_reconciler_path = repo_root / "services/reconciliation/runtime_reconciler.py"
    runbook_path = repo_root / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md"
    unresolved_fill_quarantine = _text_contains(
        fill_handler_path,
        ["unattributed_broker_fill", "_quarantine_unresolved_broker_event", "unresolved_broker_event_quarantined"],
    )
    unresolved_status_quarantine = _text_contains(
        fill_handler_path,
        ["unattributed_broker_order_status", "_resolve_order_context_from_status", "_quarantine_unresolved_broker_event"],
    )
    quarantine_summary_supported = _text_contains(
        quarantine_handler_path,
        ["active_quarantine_summary", "FROM core.core_order_quarantine", "GROUP BY reason"],
    )
    runtime_alert_supported = _text_contains(
        runtime_reconciler_path,
        ["active_quarantine_count", "active_quarantine", "runtime_reconciliation_anomaly_detected"],
    )
    runbook_supported = runbook_path.exists()
    ok = all(
        [
            unresolved_fill_quarantine,
            unresolved_status_quarantine,
            quarantine_summary_supported,
            runtime_alert_supported,
            runbook_supported,
        ]
    )
    return {
        "ok": ok,
        "unresolved_fill_quarantine": unresolved_fill_quarantine,
        "unresolved_status_quarantine": unresolved_status_quarantine,
        "quarantine_summary_supported": quarantine_summary_supported,
        "runtime_alert_supported": runtime_alert_supported,
        "runbook_supported": runbook_supported,
        "fill_handler_path": str(fill_handler_path),
        "quarantine_handler_path": str(quarantine_handler_path),
        "runtime_reconciler_path": str(runtime_reconciler_path),
        "runbook_path": str(runbook_path),
    }


def _hard_gate_ok(*gates: dict[str, Any]) -> bool:
    return all(bool(gate.get("ok")) for gate in gates)


def _day_to_audit_date(day_id: str) -> str:
    return f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"


def _audit_date_to_day_id(audit_date: str) -> str:
    return audit_date.replace("-", "")


def _previous_trading_day_id(day_id: str, *, lookback_limit: int = 10) -> str | None:
    cur = datetime.strptime(day_id, "%Y%m%d").date()
    for _ in range(max(1, lookback_limit)):
        cur = cur.fromordinal(cur.toordinal() - 1)
        if bool(is_trading_day(cur)):
            return cur.strftime("%Y%m%d")
    return None


def _latest_completed_audit_date(*, audit_root: Path, day_id: str) -> str | None:
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    candidate_dates: list[datetime.date] = []
    for path in sorted(audit_root.iterdir()) if audit_root.exists() else []:
        if not path.is_dir():
            continue
        try:
            parsed = datetime.strptime(path.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if parsed >= target_date:
            continue
        candidate_dates.append(parsed)
    if not candidate_dates:
        return None
    return max(candidate_dates).strftime("%Y-%m-%d")


def _strategy_runtime_evidence_present(*, audit_root: Path, audit_date: str, strategy_id: str) -> bool:
    audit_dir = audit_root / audit_date
    if not audit_dir.exists():
        return False
    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if strategy_id in line:
                        return True
        except OSError:
            continue
    return False


def _resolve_effective_day_id(*, audit_root: Path, day_id: str, now_et: datetime) -> tuple[str, bool]:
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    before_open_same_day = now_et.date() == target_date and now_et.timetz().replace(tzinfo=None) < time(9, 30)
    if not before_open_same_day:
        return day_id, False

    target_audit_date = _day_to_audit_date(day_id)
    if _strategy_runtime_evidence_present(
        audit_root=audit_root,
        audit_date=target_audit_date,
        strategy_id="qqq_intraday_001",
    ):
        return day_id, False

    previous_audit_date = _latest_completed_audit_date(audit_root=audit_root, day_id=day_id)
    if not previous_audit_date:
        return day_id, False
    return _audit_date_to_day_id(previous_audit_date), True


def _select_order_truth_runtime_audit_date(*, audit_root: Path, day_id: str, now_et: datetime) -> str:
    target_audit_date = _day_to_audit_date(day_id)
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    pre_open_same_day = now_et.date() == target_date and now_et.timetz().replace(tzinfo=None) < time(9, 30)
    if not pre_open_same_day:
        return target_audit_date

    candidate_dirs: list[tuple[str, datetime.date]] = []
    for path in sorted(audit_root.iterdir()) if audit_root.exists() else []:
        if not path.is_dir():
            continue
        try:
            parsed = datetime.strptime(path.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if parsed >= target_date:
            continue
        candidate_dirs.append((path.name, parsed))
    if not candidate_dirs:
        return target_audit_date
    candidate_dirs.sort(key=lambda item: item[1], reverse=True)
    return candidate_dirs[0][0]


def build_daily_trading_evidence(
    *,
    repo_root: str | Path | None = None,
    config_path: str | Path | None = None,
    day: str | None = None,
    output_path: str | Path | None = None,
    baseline_path: str | Path | None = None,
    qqq_whitelist_path: str | Path | None = None,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    current_et = now_et.astimezone(ET) if now_et is not None else datetime.now(ET)
    audit_root = root / "runtime/audit"
    requested_day_id = str(day or _today_yyyymmdd())
    requested_date = datetime.strptime(requested_day_id, "%Y%m%d").date()
    requested_day_is_trading_day = bool(is_trading_day(requested_date))
    market_day_id = requested_day_id
    premarket_refresh = False
    non_trading_day_review_previous_market_day = False
    if requested_day_is_trading_day:
        market_day_id, premarket_refresh = _resolve_effective_day_id(
            audit_root=audit_root,
            day_id=requested_day_id,
            now_et=current_et,
        )
    else:
        previous_trading_day_id = _previous_trading_day_id(requested_day_id)
        if previous_trading_day_id:
            market_day_id = previous_trading_day_id
            non_trading_day_review_previous_market_day = True
    config = Path(config_path) if config_path is not None else root / "config.yml"
    if not config.is_absolute():
        config = root / config
    baseline = Path(baseline_path) if baseline_path is not None else root / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json"
    if not baseline.is_absolute():
        baseline = root / baseline
    output = Path(output_path) if output_path is not None else root / f"reports/daily_trading_evidence/{requested_day_id}.json"
    if not output.is_absolute():
        output = root / output

    live_report_path = root / f"reports/strategy_surface/live_strategy_surface_report_{requested_day_id}.json"
    uom_report_path = root / f"reports/orders/uom_caller_surface_{requested_day_id}.json"
    qqq_drift_path = root / "reports/qqq_kernel_shadow_parity/qqq_shadow_drift_report_latest.json"
    order_truth_runtime_path = root / f"reports/order_truth_runtime/order_truth_runtime_{requested_day_id}.json"
    runtime_reconciliation_path = root / f"reports/runtime_reconciliation/runtime_reconciliation_health_{requested_day_id}.json"

    live_report = build_live_strategy_surface_report(config, repo_root=root)
    _write_json(live_report_path, live_report)
    baseline_report = _read_json(baseline) if baseline.exists() else {}
    phase0_gate = evaluate_phase0_freeze_gate(
        live_report,
        baseline=baseline_report or None,
        require_baseline=True,
    )

    uom_report = build_uom_caller_surface_report(root)
    _write_json(uom_report_path, uom_report)
    uom_gate = evaluate_uom_caller_gate(uom_report)

    whitelist = Path(qqq_whitelist_path) if qqq_whitelist_path else root / "reports/qqq_kernel_shadow_parity/qqq_shadow_drift_whitelist.json"
    qqq_drift = build_qqq_shadow_drift_report(
        snapshot_root=root / "reports/qqq_signal_snapshots",
        parity_root=root / "reports/qqq_kernel_shadow_parity",
        whitelist_path=whitelist if whitelist.exists() else None,
    )
    _write_json(qqq_drift_path, qqq_drift)
    order_truth_runtime_audit_date = _select_order_truth_runtime_audit_date(
        audit_root=audit_root,
        day_id=market_day_id,
        now_et=current_et,
    )
    order_truth_runtime = build_order_truth_runtime_evidence(
        audit_root=audit_root,
        date=order_truth_runtime_audit_date,
    )
    order_truth_runtime["requested_day"] = requested_day_id
    order_truth_runtime["market_day"] = market_day_id
    order_truth_runtime["audit_date"] = order_truth_runtime_audit_date
    order_truth_runtime["used_previous_completed_day"] = order_truth_runtime_audit_date != _day_to_audit_date(requested_day_id)
    _write_json(order_truth_runtime_path, order_truth_runtime)
    runtime_reconciliation = build_runtime_reconciliation_health(
        audit_root=audit_root,
        day=requested_day_id,
        now_et=current_et,
    )
    _write_json(runtime_reconciliation_path, runtime_reconciliation)

    protection_registry = _build_protection_registry_evidence(root)
    position_reconciler = _build_position_reconciler_evidence(root)
    quarantine_evidence = _build_quarantine_evidence(root)
    order_truth_runtime_gate = {
        "ok": bool(order_truth_runtime.get("ok")),
        "strategy_row_count": int(order_truth_runtime.get("strategy_row_count") or 0),
        "false_block_removed": bool(order_truth_runtime.get("false_block_removed")),
        "legacy_open_orders_block_count": int(order_truth_runtime.get("legacy_open_orders_block_count") or 0),
        "legitimate_block_observed": bool(order_truth_runtime.get("legitimate_block_observed")),
        "recommendation": str(order_truth_runtime.get("recommendation") or "HOLD"),
        "reasons": list(order_truth_runtime.get("reasons") or []),
        "audit_date": str(order_truth_runtime.get("audit_date") or _day_to_audit_date(market_day_id)),
        "used_previous_completed_day": bool(order_truth_runtime.get("used_previous_completed_day")),
    }
    hard_gate_ok = _hard_gate_ok(
        phase0_gate,
        uom_gate,
        protection_registry,
        position_reconciler,
        quarantine_evidence,
        order_truth_runtime_gate,
        runtime_reconciliation,
    )
    qqq_unresolved = int(qqq_drift.get("unresolved_diff_count") or 0)
    qqq_recommendation = str(qqq_drift.get("recommendation") or "HOLD")
    qqq_no_unresolved = qqq_unresolved == 0 and qqq_recommendation != "HOLD"
    paper_clean_candidate = hard_gate_ok and qqq_no_unresolved
    live_cutover_candidate = paper_clean_candidate and qqq_recommendation == "PASS"

    blockers: list[str] = []
    if not phase0_gate.get("ok"):
        blockers.extend(f"phase0:{item}" for item in phase0_gate.get("blocking_reasons", []))
    if not uom_gate.get("ok"):
        blockers.extend(f"uom_caller:{item}" for item in uom_gate.get("blocking_files", []))
    if not protection_registry.get("ok"):
        blockers.append("protection_registry_evidence_incomplete")
    if not position_reconciler.get("ok"):
        blockers.append("position_reconciler_lot_level_evidence_incomplete")
    if not quarantine_evidence.get("ok"):
        blockers.append("order_quarantine_evidence_incomplete")
    if not order_truth_runtime_gate.get("ok"):
        blockers.append("order_truth_runtime_evidence_incomplete_or_false_block_persists")
    if not runtime_reconciliation.get("ok"):
        blockers.extend(f"runtime_reconciliation:{item}" for item in runtime_reconciliation.get("blockers", []))
    if qqq_recommendation == "HOLD":
        blockers.append("qqq_shadow_drift_hold")

    if blockers:
        recommendation = "HOLD"
    elif qqq_recommendation == "CONTINUE_SHADOW":
        recommendation = "CONTINUE_SHADOW"
    else:
        recommendation = "PASS"

    payload = {
        "generated_at": current_et.isoformat(timespec="seconds"),
        "day": requested_day_id,
        "requested_day": requested_day_id,
        "market_day": market_day_id,
        "requested_day_is_trading_day": requested_day_is_trading_day,
        "premarket_refresh_used_previous_market_day": premarket_refresh,
        "non_trading_day_review_previous_market_day": non_trading_day_review_previous_market_day,
        "repo_root": str(root),
        "recommendation": recommendation,
        "blockers": sorted(set(blockers)),
        "paper_clean_candidate": paper_clean_candidate,
        "live_cutover_candidate": live_cutover_candidate,
        "reports": {
            "live_strategy_surface": str(live_report_path),
            "uom_caller_surface": str(uom_report_path),
            "qqq_shadow_drift": str(qqq_drift_path),
            "order_truth_runtime": str(order_truth_runtime_path),
            "runtime_reconciliation": str(runtime_reconciliation_path),
            "phase0_baseline": str(baseline),
        },
        "gates": {
            "phase0": phase0_gate,
            "uom_caller": uom_gate,
            "qqq_shadow_drift": {
                "recommendation": qqq_recommendation,
                "d3_ready": bool(qqq_drift.get("d3_ready")),
                "observed_days": list(qqq_drift.get("observed_days") or []),
                "unresolved_diff_count": qqq_unresolved,
                "whitelist_entry_count": int(qqq_drift.get("whitelist_entry_count") or 0),
                "whitelist_ok": bool(qqq_drift.get("whitelist_ok")),
                "needs_48h_attribution_count": int(qqq_drift.get("needs_48h_attribution_count") or 0),
            },
            "protection_registry": protection_registry,
            "position_reconciler": position_reconciler,
            "order_quarantine": quarantine_evidence,
            "order_truth_runtime": order_truth_runtime_gate,
            "runtime_reconciliation": runtime_reconciliation,
        },
    }
    _write_json(output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build daily trading rollout evidence package")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--day", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--qqq-whitelist", default="")
    parser.add_argument("--fail-on-hold", action="store_true")
    args = parser.parse_args(argv)
    report = build_daily_trading_evidence(
        repo_root=args.repo_root or None,
        config_path=args.config,
        day=args.day or None,
        output_path=args.output or None,
        baseline_path=args.baseline or None,
        qqq_whitelist_path=args.qqq_whitelist or None,
    )
    print(
        json.dumps(
            {
                "day": report["day"],
                "recommendation": report["recommendation"],
                "paper_clean_candidate": report["paper_clean_candidate"],
                "live_cutover_candidate": report["live_cutover_candidate"],
                "blocker_count": len(report["blockers"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if args.fail_on_hold and report["recommendation"] == "HOLD":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
