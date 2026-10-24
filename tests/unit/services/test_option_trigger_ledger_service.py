from __future__ import annotations

import json
from datetime import datetime

import services.production.option_trigger_ledger_service as option_trigger_ledger_service
from services.production.option_trigger_ledger_service import build_option_trigger_ledger


def _append_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_option_trigger_ledger_keeps_selected_trigger_active_until_lifecycle_terminal(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        [
            {
                "trigger_key": "trigger-1",
                "trigger_id": "trigger-1",
                "source_strategy_id": "qqq_intraday_001",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "underlying": "QQQ",
                "signal_family": "qqq",
                "status": "received",
                "reason": "stock_signal",
                "ts": "2026-05-05T10:00:00-04:00",
                "terminal": False,
            }
        ],
    )
    _append_jsonl(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_20260505.jsonl",
        [
            {
                "artifact_type": "contract_selection_verdict",
                "generated_at": "2026-05-05T10:00:05-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "symbol": "QQQ",
                "status": "selected",
                "reason": "contract_selected",
            }
        ],
    )

    status = build_option_trigger_ledger(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:10-04:00"),
        required_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["recent_trigger_count"] == 1
    assert status["active_trigger_count"] == 1
    assert status["selected_trigger_count"] == 1
    assert status["triggers"][0]["status"] == "selected"
    assert status["triggers"][0]["terminal"] is False


def test_option_trigger_ledger_marks_filled_lifecycle_terminal(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        [
            {
                "trigger_key": "trigger-1",
                "trigger_id": "trigger-1",
                "source_strategy_id": "qqq_intraday_001",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "underlying": "QQQ",
                "signal_family": "qqq",
                "status": "received",
                "reason": "stock_signal",
                "ts": "2026-05-05T10:00:00-04:00",
                "terminal": False,
            }
        ],
    )
    _append_jsonl(
        tmp_path / "runtime/state/option_trigger_lifecycle.jsonl",
        [
            {
                "trigger_key": "trigger-1",
                "trigger_id": "trigger-1",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "status": "filled",
                "reason": "broker_fill_verified",
                "order_id": "order-1",
                "broker_order_id": "1001",
                "exec_id": "exec-1",
                "generated_at": "2026-05-05T10:00:08-04:00",
            }
        ],
    )

    status = build_option_trigger_ledger(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:10-04:00"),
        required_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "go"
    assert status["active_trigger_count"] == 0
    assert status["triggers"][0]["status"] == "filled"
    assert status["triggers"][0]["terminal"] is True
    assert status["triggers"][0]["exec_id"] == "exec-1"


def test_option_trigger_ledger_blocks_recent_trigger_without_terminal_verdict(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        [
            {
                "trigger_key": "trigger-1",
                "trigger_id": "trigger-1",
                "source_strategy_id": "qqq_intraday_001",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "underlying": "QQQ",
                "signal_family": "qqq",
                "status": "received",
                "reason": "stock_signal",
                "ts": "2026-05-05T10:00:00-04:00",
                "terminal": False,
            }
        ],
    )

    status = build_option_trigger_ledger(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:10-04:00"),
        required_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["active_trigger_count"] == 1
    assert status["blockers"] == ["option_trigger_active_without_terminal_verdict"]


def test_option_trigger_ledger_blocks_recent_rejection(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        [
            {
                "trigger_key": "trigger-1",
                "trigger_id": "trigger-1",
                "source_strategy_id": "qqq_intraday_001",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "underlying": "QQQ",
                "signal_family": "qqq",
                "status": "received",
                "reason": "stock_signal",
                "ts": "2026-05-05T10:00:00-04:00",
                "terminal": False,
            }
        ],
    )
    _append_jsonl(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_20260505.jsonl",
        [
            {
                "artifact_type": "contract_selection_verdict",
                "generated_at": "2026-05-05T10:00:05-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "symbol": "QQQ",
                "status": "rejected",
                "reason": "no_contract_selection",
            }
        ],
    )

    status = build_option_trigger_ledger(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:10-04:00"),
        required_strategy_ids=["qqq_intraday_option_v1_001"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["active_trigger_count"] == 0
    assert status["rejected_trigger_count"] == 1
    assert status["blockers"] == ["option_trigger_rejected_contract_selection"]


def test_option_trigger_ledger_expires_old_unresolved_trigger_instead_of_dropping_it(tmp_path):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        [
            {
                "trigger_key": "trigger-old",
                "trigger_id": "trigger-old",
                "source_strategy_id": "qqq_intraday_001",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "underlying": "QQQ",
                "signal_family": "qqq",
                "status": "received",
                "reason": "stock_signal",
                "ts": "2026-05-05T09:00:00-04:00",
                "terminal": False,
            }
        ],
    )

    status = build_option_trigger_ledger(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        required_strategy_ids=["qqq_intraday_option_v1_001"],
        max_age_sec=300,
    )

    assert status["go_no_go"] == "go"
    assert status["recent_trigger_count"] == 1
    assert status["expired_trigger_count"] == 1
    assert status["triggers"][0]["status"] == "expired"
    assert status["triggers"][0]["stale_unresolved"] is True
    lifecycle_rows = [
        json.loads(line)
        for line in (tmp_path / "runtime/state/option_trigger_lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lifecycle_rows[-1]["trigger_key"] == "trigger-old"
    assert lifecycle_rows[-1]["status"] == "expired"
    assert lifecycle_rows[-1]["terminal"] is True


def test_option_trigger_ledger_blocks_when_expiry_lifecycle_write_fails(tmp_path, monkeypatch):
    _append_jsonl(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        [
            {
                "trigger_key": "trigger-old",
                "trigger_id": "trigger-old",
                "source_strategy_id": "qqq_intraday_001",
                "consumer_strategy_id": "qqq_intraday_option_v1_001",
                "underlying": "QQQ",
                "signal_family": "qqq",
                "status": "received",
                "reason": "stock_signal",
                "ts": "2026-05-05T09:00:00-04:00",
                "terminal": False,
            }
        ],
    )
    monkeypatch.setattr(option_trigger_ledger_service, "_append_expired_lifecycle_event_once", lambda **_kwargs: None)

    status = build_option_trigger_ledger(
        repo_root=tmp_path,
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        required_strategy_ids=["qqq_intraday_option_v1_001"],
        max_age_sec=300,
    )

    assert status["go_no_go"] == "no_go"
    assert status["active_trigger_count"] == 1
    assert status["expired_trigger_count"] == 0
    assert status["triggers"][0]["status"] == "expiry_lifecycle_write_failed"
    assert status["triggers"][0]["terminal"] is False
