from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from libs.strategy.kernel_contracts.types import Selection
from services.production.contract_selection_service import build_contract_selection_status, select_option_contract_for_entry


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def test_platform_contract_selection_boundary_wraps_legacy_plugin_selector():
    class _Plugin:
        component_name = "legacy_option_plugin"

        def __init__(self):
            self.calls = 0

        def select_contract_pure(self, runtime, entry_plan):
            self.calls += 1
            return Selection(
                opt_symbol="OPT:QQQ:20260505:682:C",
                expiry="2026-05-05",
                strike=682.0,
                side="CALL",
                bid=1.0,
                ask=1.1,
                mid=1.05,
                payload={},
            )

    plugin = _Plugin()
    result = select_option_contract_for_entry(
        runtime=SimpleNamespace(now=datetime.fromisoformat("2026-05-05T10:00:00-04:00")),
        entry_plan=SimpleNamespace(side="CALL", contracts=1),
        plugin=plugin,
        strategy_id="qqq_intraday_option_v1_001",
        symbol="QQQ",
        kernel_mode="pure",
    )

    assert plugin.calls == 1
    assert result.selection is not None
    assert result.status == "selected"
    assert result.reason == "contract_selected"
    assert result.legacy_plugin_selection is True
    assert result.payload["platform_service"] == "select_option_contract_for_entry"
    assert result.payload["selected_contract_identity_valid"] is True
    assert result.payload["selected_contract"]["multiplier"] == 100
    assert result.payload["selected_contract"]["exchange"] == "SMART"
    assert result.payload["selected_contract"]["tradingClass"] == "QQQ"
    assert result.payload["selected_contract"]["localSymbol"] == "QQQ   260505C00682000"
    assert result.payload["missing_contract_identity_fields"] == []


def test_platform_contract_selection_rejects_legacy_plugin_selector_when_required(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("REQUIRE_PLATFORM_OPTION_CONTRACT_SELECTION", "1")
    monkeypatch.delenv("ALLOW_LEGACY_OPTION_CONTRACT_SELECTION", raising=False)

    class _Plugin:
        component_name = "legacy_option_plugin"

        def __init__(self):
            self.calls = 0

        def select_contract_pure(self, runtime, entry_plan):
            self.calls += 1
            return SimpleNamespace(opt_symbol="OPT:QQQ:20260505:682:C")

    plugin = _Plugin()
    result = select_option_contract_for_entry(
        runtime=SimpleNamespace(now=datetime.fromisoformat("2026-05-05T10:00:00-04:00")),
        entry_plan=SimpleNamespace(side="CALL", contracts=1),
        plugin=plugin,
        strategy_id="qqq_intraday_option_v1_001",
        symbol="QQQ",
        kernel_mode="pure",
    )

    assert plugin.calls == 0
    assert result.selection is None
    assert result.status == "rejected"
    assert result.reason == "legacy_plugin_contract_selection_disabled"
    assert result.legacy_plugin_selection is True


def test_platform_contract_selection_completes_internal_symbol_identity_in_strict_mode(monkeypatch):
    monkeypatch.setenv("REQUIRE_COMPLETE_OPTION_CONTRACT_IDENTITY", "1")

    class _Plugin:
        component_name = "platform_option_plugin"

        def select_contract_from_preferences_pure(self, runtime, entry_plan, preferences):
            return Selection(
                opt_symbol="OPT:QQQ:20260505:682:C",
                expiry="2026-05-05",
                strike=682.0,
                side="CALL",
                bid=1.0,
                ask=1.1,
                mid=1.05,
            )

    result = select_option_contract_for_entry(
        runtime=SimpleNamespace(now=datetime.fromisoformat("2026-05-05T10:00:00-04:00")),
        entry_plan=SimpleNamespace(side="CALL", contracts=1),
        plugin=_Plugin(),
        strategy_id="qqq_intraday_option_v1_001",
        symbol="QQQ",
        kernel_mode="pure",
    )

    assert result.selection is not None
    assert result.status == "selected"
    assert result.reason == "contract_selected"
    assert result.payload["selected_contract_identity_valid"] is True
    assert result.payload["selected_contract"]["localSymbol"] == "QQQ   260505C00682000"


def test_contract_selection_status_accepts_valid_selected_contract(tmp_path):
    _write_json(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_latest.json",
        {
            "artifact_type": "contract_selection_verdict",
            "generated_at": "2026-05-05T10:00:05-04:00",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "QQQ",
            "status": "selected",
            "reason": "contract_selected",
            "payload": {
                "selection": {
                    "opt_symbol": "OPT:QQQ:20260505:682:C",
                    "expiry": "2026-05-05",
                    "strike": 682.0,
                    "side": "CALL",
                    "multiplier": 100,
                    "exchange": "SMART",
                    "tradingClass": "QQQ",
                    "conId": 123456789,
                    "localSymbol": "QQQ   260505C00682000",
                }
            },
        },
    )

    status = build_contract_selection_status(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:20-04:00"),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "go"
    assert status["ok"] is True
    assert "process_start_time" in status
    assert "git_head" in status
    assert "config_hash" in status
    assert status["selected_contract_identity_valid"] is True
    assert status["selected_contract"]["opt_symbol"] == "OPT:QQQ:20260505:682:C"
    assert status["missing_contract_identity_fields"] == []


def test_contract_selection_status_blocks_invalid_selected_contract_identity(tmp_path):
    _write_json(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_latest.json",
        {
            "artifact_type": "contract_selection_verdict",
            "generated_at": "2026-05-05T10:00:05-04:00",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "QQQ",
            "status": "selected",
            "reason": "contract_selected",
            "payload": {
                "selection": {
                    "opt_symbol": "OPT:QQQ:20260504:682:C",
                    "expiry": "2026-05-05",
                    "strike": 682.0,
                    "side": "CALL",
                    "multiplier": 100,
                    "exchange": "SMART",
                    "tradingClass": "QQQ",
                    "conId": 123456789,
                }
            },
        },
    )

    status = build_contract_selection_status(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:20-04:00"),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["selected_contract_identity_valid"] is False
    assert status["blockers"] == ["selected_contract_identity_invalid"]


def test_contract_selection_status_completes_missing_broker_contract_identity(tmp_path):
    _write_json(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_latest.json",
        {
            "artifact_type": "contract_selection_verdict",
            "generated_at": "2026-05-05T10:00:05-04:00",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "QQQ",
            "status": "selected",
            "reason": "contract_selected",
            "payload": {
                "selection": {
                    "opt_symbol": "OPT:QQQ:20260505:682:C",
                    "expiry": "2026-05-05",
                    "strike": 682.0,
                    "side": "CALL",
                }
            },
        },
    )

    status = build_contract_selection_status(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:20-04:00"),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "go"
    assert status["selected_contract_identity_valid"] is True
    assert status["selected_contract"]["multiplier"] == 100
    assert status["selected_contract"]["exchange"] == "SMART"
    assert status["selected_contract"]["tradingClass"] == "QQQ"
    assert status["selected_contract"]["localSymbol"] == "QQQ   260505C00682000"
    assert status["missing_contract_identity_fields"] == []


def test_contract_selection_status_ignores_stale_selected_identity_without_recent_handoff(tmp_path):
    _write_json(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_latest.json",
        {
            "artifact_type": "contract_selection_verdict",
            "generated_at": "2026-03-25T10:00:05-04:00",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "QQQ",
            "status": "selected",
            "reason": "contract_selected",
            "payload": {
                "selection": {
                    "opt_symbol": "OPT:QQQ:20260325:682:C",
                    "expiry": "2026-03-25",
                    "strike": 682.0,
                    "side": "CALL",
                }
            },
        },
    )

    status = build_contract_selection_status(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:20-04:00"),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "go"
    assert status["stale"] is True
    assert status["stale_payload_ignored"] is True
    assert status["selected_contract_identity_valid"] is None
    assert status["selected_contract"] == {}
    assert status["missing_contract_identity_fields"] == []
    assert status["payload"] == {}
    assert status["blockers"] == []


def test_contract_selection_status_blocks_missing_verdict_for_recent_required_handoff(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        {
            "trigger_key": "trigger-1",
            "trigger_id": "trigger-1",
            "source_strategy_id": "qqq_intraday_001",
            "consumer_strategy_id": "qqq_intraday_option_v1_001",
            "status": "published",
            "ts": "2026-05-05T10:00:00-04:00",
            "terminal": False,
        },
    )

    status = build_contract_selection_status(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:20-04:00"),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["reason"] == "missing_contract_selection_verdict"
    assert status["recent_option_handoff"]["recent_required_event"] is True


def test_contract_selection_scans_recent_required_handoff_not_only_last_line(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        {
            "trigger_key": "trigger-required",
            "trigger_id": "trigger-required",
            "source_strategy_id": "qqq_intraday_001",
            "consumer_strategy_id": "qqq_intraday_option_v1_001",
            "status": "published",
            "ts": "2026-05-05T10:00:00-04:00",
            "terminal": False,
        },
    )
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        {
            "trigger_key": "trigger-other",
            "trigger_id": "trigger-other",
            "source_strategy_id": "other",
            "consumer_strategy_id": "non_required_option_strategy",
            "status": "expired",
            "ts": "2026-05-05T10:00:05-04:00",
            "terminal": True,
        },
    )

    status = build_contract_selection_status(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:20-04:00"),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["recent_option_handoff"]["recent_required_event"] is True
    assert status["recent_option_handoff"]["trigger_key"] == "trigger-required"
