from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tool.ops.build_daily_trading_evidence as daily_evidence
from tool.ops.build_daily_trading_evidence import build_daily_trading_evidence

ET = ZoneInfo("America/New_York")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_daily_trading_evidence_builds_machine_readable_rollout_package(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yml",
        """
strategies:
  - id: qqq_intraday_001
    enabled: true
    module: strategies.qqq_intraday_strategy
    class: QQQ
    params:
      extras:
        symbol: QQQ
        enable_shared_underlying_data_plane: true
        shared_underlying_read_mode: shadow_compare
""",
    )
    _write(tmp_path / "strategies/qqq_intraday_strategy.py", "def on_features():\n    pass\n")
    baseline = {
        "live_strategy_ids": ["qqq_intraday_001"],
        "shadow_strategy_ids": [],
        "summary": {"live_strategy_count": 1},
        "strategies": [{"strategy_id": "qqq_intraday_001", "mode": "live", "risk_flags": ["live_strategy"]}],
    }
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps(baseline),
    )
    _write(
        tmp_path / "reports/qqq_signal_snapshots/20260419.jsonl",
        json.dumps({"ts_et": "2026-04-19T10:00:00", "signal": {}}) + "\n",
    )
    _write(
        tmp_path / "reports/qqq_signal_snapshots/20260420.jsonl",
        json.dumps({"ts_et": "2026-04-20T10:00:00", "signal": {}}) + "\n",
    )
    _write(
        tmp_path / "reports/qqq_signal_snapshots/20260421.jsonl",
        json.dumps({"ts_et": "2026-04-21T10:00:00", "signal": {}}) + "\n",
    )
    _write(
        tmp_path / "reports/qqq_kernel_shadow_parity/parity_20260421.json",
        json.dumps(
            {
                "day": "20260421",
                "diff_total": 0,
                "match_ratio": 1.0,
                "legacy_count": 1,
                "kernel_count": 1,
                "rows": [{"key": "decision:1", "matched": True, "diffs": []}],
            }
        ),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(
        tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md",
        "# runbook\n",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-21/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_blocked",
                "reason": "sign_mismatch",
                "payload": {},
            }
        )
        + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260421",
        output_path=tmp_path / "reports/daily_trading_evidence/20260421.json",
    )

    assert report["recommendation"] == "PASS"
    assert report["paper_clean_candidate"] is True
    assert report["live_cutover_candidate"] is True
    assert report["blockers"] == []
    assert Path(report["reports"]["live_strategy_surface"]).exists()
    assert Path(report["reports"]["uom_caller_surface"]).exists()
    assert Path(report["reports"]["qqq_shadow_drift"]).exists()


def test_daily_trading_evidence_uses_previous_trading_day_on_non_trading_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        daily_evidence,
        "is_trading_day",
        lambda d: d.isoformat() == "2026-04-24",
    )
    _write(tmp_path / "config.yml", "strategies: []\n")
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps({"live_strategy_ids": [], "shadow_strategy_ids": [], "summary": {"live_strategy_count": 0}, "strategies": []}),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(
        tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md",
        "# runbook\n",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        json.dumps({"strategy_id": "qqq_intraday_001", "stage": "gate_blocked", "reason": "sign_mismatch", "payload": {}}) + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260425",
        output_path=tmp_path / "reports/daily_trading_evidence/20260425.json",
        now_et=datetime(2026, 4, 25, 10, 0, tzinfo=ET),
    )

    assert report["day"] == "20260425"
    assert report["market_day"] == "20260424"
    assert report["requested_day_is_trading_day"] is False
    assert report["non_trading_day_review_previous_market_day"] is True
    assert report["gates"]["order_truth_runtime"]["audit_date"] == "2026-04-24"


def test_daily_trading_evidence_holds_when_phase0_baseline_missing(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yml",
        """
strategies: []
""",
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(
        tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md",
        "# runbook\n",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-21/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_blocked",
                "reason": "sign_mismatch",
                "payload": {},
            }
        )
        + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260421",
        output_path=tmp_path / "reports/daily_trading_evidence/20260421.json",
    )

    assert report["recommendation"] == "HOLD"
    assert "phase0:phase0_freeze_baseline_missing" in report["blockers"]


def test_daily_trading_evidence_holds_when_order_quarantine_evidence_missing(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yml",
        """
strategies: []
""",
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260421",
        output_path=tmp_path / "reports/daily_trading_evidence/20260421.json",
    )

    assert report["recommendation"] == "HOLD"
    assert "order_quarantine_evidence_incomplete" in report["blockers"]


def test_daily_trading_evidence_holds_when_order_truth_runtime_false_block_persists(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yml",
        """
strategies: []
""",
    )
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps({"live_strategy_ids": [], "shadow_strategy_ids": [], "summary": {"live_strategy_count": 0}, "strategies": []}),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(
        tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md",
        "# runbook\n",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-21/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_001",
                "stage": "entry_blocked_legacy_open_orders",
                "reason": "legacy_open_orders_block",
                "payload": {},
            }
        )
        + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260421",
        output_path=tmp_path / "reports/daily_trading_evidence/20260421.json",
    )

    assert report["recommendation"] == "HOLD"
    assert "order_truth_runtime_evidence_incomplete_or_false_block_persists" in report["blockers"]


def test_daily_trading_evidence_accepts_option_live_alias_for_order_truth_runtime(tmp_path: Path) -> None:
    _write(tmp_path / "config.yml", "strategies: []\n")
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps({"live_strategy_ids": [], "shadow_strategy_ids": [], "summary": {"live_strategy_count": 0}, "strategies": []}),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(
        tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md",
        "# runbook\n",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-21/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "position_sync_qty_resolution",
                "payload": {"qty_resolved": {"TQQQ": 0.0, "SQQQ": 0.0}},
            }
        )
        + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260421",
        output_path=tmp_path / "reports/daily_trading_evidence/20260421.json",
    )

    gate = report["gates"]["order_truth_runtime"]
    assert gate["ok"] is True
    assert "order_truth_runtime_evidence_incomplete_or_false_block_persists" not in report["blockers"]


def test_daily_trading_evidence_uses_previous_completed_day_for_runtime_evidence_before_open(tmp_path: Path) -> None:
    _write(tmp_path / "config.yml", "strategies: []\n")
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps({"live_strategy_ids": [], "shadow_strategy_ids": [], "summary": {"live_strategy_count": 0}, "strategies": []}),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md", "# runbook\n")
    _write(
        tmp_path / "runtime/audit/2026-04-23/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_blocked",
                "reason": "sign_mismatch",
                "payload": {},
            }
        )
        + "\n",
    )
    _write(
        tmp_path / "reports/qqq_signal_snapshots/20260423.jsonl",
        json.dumps({"ts_et": "2026-04-23T10:00:00", "signal": {}}) + "\n",
    )
    _write(
        tmp_path / "reports/qqq_kernel_shadow_parity/parity_20260423.json",
        json.dumps(
            {
                "day": "20260423",
                "diff_total": 0,
                "match_ratio": 1.0,
                "legacy_count": 1,
                "kernel_count": 1,
                "rows": [{"key": "decision:1", "matched": True, "diffs": []}],
            }
        ),
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 15, tzinfo=ET),
    )

    assert report["day"] == "20260424"
    assert report["requested_day"] == "20260424"
    assert report["market_day"] == "20260423"
    assert report["premarket_refresh_used_previous_market_day"] is True
    gate = report["gates"]["order_truth_runtime"]
    assert gate["ok"] is True
    assert gate["audit_date"] == "2026-04-23"
    assert gate["used_previous_completed_day"] is True
    assert "order_truth_runtime_evidence_incomplete_or_false_block_persists" not in report["blockers"]
    assert Path(report["reports"]["order_truth_runtime"]).name == "order_truth_runtime_20260424.json"


def test_daily_trading_evidence_keeps_same_day_before_open_when_runtime_evidence_exists(tmp_path: Path) -> None:
    _write(tmp_path / "config.yml", "strategies: []\n")
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps({"live_strategy_ids": [], "shadow_strategy_ids": [], "summary": {"live_strategy_count": 0}, "strategies": []}),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md", "# runbook\n")
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_blocked",
                "reason": "sign_mismatch",
                "payload": {},
            }
        )
        + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 15, tzinfo=ET),
    )

    assert report["day"] == "20260424"
    assert report["requested_day"] == "20260424"
    assert report["premarket_refresh_used_previous_market_day"] is False


def test_daily_trading_evidence_does_not_fallback_after_open(tmp_path: Path) -> None:
    _write(tmp_path / "config.yml", "strategies: []\n")
    _write(
        tmp_path / "reports/strategy_surface/live_strategy_surface_freeze_baseline.json",
        json.dumps({"live_strategy_ids": [], "shadow_strategy_ids": [], "summary": {"live_strategy_count": 0}, "strategies": []}),
    )
    _write(tmp_path / "db/pg/migration_20260421_uom_position_ledger.ddl", "core_protection_order_registry")
    _write(
        tmp_path / "services/orders/manager.py",
        "_register_protection_order\ncore.core_protection_order_registry\n_is_protection_intent\n",
    )
    _write(
        tmp_path / "tests/unit/services/test_unified_order_manager.py",
        "test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers\ntake_profit\n",
    )
    _write(
        tmp_path / "services/reconciliation/contracts.py",
        "GROUP BY symbol, strategy_id, lot_id\nledger_lots\nledger_lot_count_by_symbol\n",
    )
    _write(
        tmp_path / "services/exec/fill_event_handler.py",
        "unattributed_broker_fill\n_quarantine_unresolved_broker_event\nunresolved_broker_event_quarantined\n"
        "unattributed_broker_order_status\n_resolve_order_context_from_status\n",
    )
    _write(
        tmp_path / "services/reconciliation/order_quarantine.py",
        "active_quarantine_summary\nFROM core.core_order_quarantine\nGROUP BY reason\n",
    )
    _write(
        tmp_path / "services/reconciliation/runtime_reconciler.py",
        "active_quarantine_count\nactive_quarantine\nruntime_reconciliation_anomaly_detected\n",
    )
    _write(tmp_path / "docs/implementation/ORDER_TRUTH_RUNTIME_RUNBOOK_2026-04-23.md", "# runbook\n")
    _write(
        tmp_path / "runtime/audit/2026-04-23/strategy-10.jsonl",
        json.dumps(
            {
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_blocked",
                "reason": "sign_mismatch",
                "payload": {},
            }
        )
        + "\n",
    )

    report = build_daily_trading_evidence(
        repo_root=tmp_path,
        config_path=tmp_path / "config.yml",
        day="20260424",
        output_path=tmp_path / "reports/daily_trading_evidence/20260424.json",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=ET),
    )

    gate = report["gates"]["order_truth_runtime"]
    assert gate["ok"] is False
    assert gate["audit_date"] == "2026-04-24"
    assert gate["used_previous_completed_day"] is False
    assert "order_truth_runtime_evidence_incomplete_or_false_block_persists" in report["blockers"]
