import json
from pathlib import Path

from scripts.check_qqq_option_signal_runtime_acceptance import main


def _w(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_runtime_acceptance_pass_with_expected_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    date = "2026-04-09"
    audit_dir = tmp_path / "runtime" / "audit" / date
    strategy_file = audit_dir / "strategy-001.jsonl"
    _w(
        strategy_file,
        [
            {
                "ts": "2026-04-09T09:31:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_evaluated",
                "status": "entry_allowed",
            },
            {
                "ts": "2026-04-09T09:31:01-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "stock_signal_publish_ok",
                "status": "published",
            },
            {
                "ts": "2026-04-09T09:31:10-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "feature_callback_heartbeat",
                "status": "alive",
            },
            {
                "ts": "2026-04-09T09:32:00-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "plugin_intent_submit_result",
                "reason": "entry_intent_submitted",
                "payload": {
                    "plugin_payload": {
                        "signal": {
                            "payload": {
                                "signal_source_mode": "upstream_stock_signal",
                            }
                        }
                    }
                },
            },
        ],
    )
    plugin_log = tmp_path / "plugin.jsonl"
    _w(
        plugin_log,
        [
            {
                "event": "qqq_intraday_option_minute_heartbeat",
                "strategy_id": "qqq_intraday_option_v1_001",
                "signal_plane_backend_config": "auto",
                "signal_plane_state": "healthy_redis",
                "signal_plane_redis_ok": True,
            },
            {
                "event": "qqq_intraday_option_downstream_result",
                "strategy_id": "qqq_intraday_option_v1_001",
                "result_code": "signal_consumed",
            }
        ],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_qqq_option_signal_runtime_acceptance.py",
            "--date",
            date,
            "--audit-dir",
            str(tmp_path / "runtime" / "audit"),
            "--plugin-log-glob",
            str(plugin_log),
        ],
    )
    code = main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert code == 0
    assert payload["status"] == "PASS"


def test_runtime_acceptance_fail_without_signal_mode(tmp_path: Path, monkeypatch, capsys) -> None:
    date = "2026-04-09"
    audit_dir = tmp_path / "runtime" / "audit" / date
    strategy_file = audit_dir / "strategy-001.jsonl"
    _w(
        strategy_file,
        [
            {
                "ts": "2026-04-09T09:31:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_evaluated",
                "status": "entry_allowed",
            },
            {
                "ts": "2026-04-09T09:31:01-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "stock_signal_publish_ok",
                "status": "published",
            },
            {
                "ts": "2026-04-09T09:31:10-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "feature_callback_heartbeat",
                "status": "alive",
            },
        ],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_qqq_option_signal_runtime_acceptance.py",
            "--date",
            date,
            "--audit-dir",
            str(tmp_path / "runtime" / "audit"),
            "--plugin-log-glob",
            str(tmp_path / "missing*.jsonl"),
        ],
    )
    code = main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert code == 2
    assert payload["status"] == "FAIL"


def test_runtime_acceptance_reads_default_trader_log_fallback(tmp_path: Path, monkeypatch, capsys) -> None:
    date = "2026-04-09"
    audit_dir = tmp_path / "runtime" / "audit" / date
    strategy_file = audit_dir / "strategy-001.jsonl"
    _w(
        strategy_file,
        [
            {
                "ts": "2026-04-09T09:31:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_evaluated",
                "status": "entry_allowed",
            },
            {
                "ts": "2026-04-09T09:31:01-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "stock_signal_publish_ok",
                "status": "published",
            },
            {
                "ts": "2026-04-09T09:31:10-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "feature_callback_heartbeat",
                "status": "alive",
            },
            {
                "ts": "2026-04-09T09:32:00-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "plugin_intent_submit_result",
                "reason": "entry_intent_submitted",
                "payload": {
                    "plugin_payload": {
                        "signal": {
                            "payload": {
                                "signal_source_mode": "upstream_stock_signal",
                            }
                        }
                    }
                },
            },
        ],
    )
    trader_log = tmp_path / "trader.log"
    _w(
        trader_log,
        [
            {
                "event": "qqq_intraday_option_minute_heartbeat",
                "strategy_id": "qqq_intraday_option_v1_001",
                "signal_plane_backend_config": "auto",
                "signal_plane_state": "healthy_redis",
                "signal_plane_redis_ok": True,
            },
            {
                "event": "qqq_intraday_option_downstream_result",
                "strategy_id": "qqq_intraday_option_v1_001",
                "result_code": "signal_consumed",
            },
        ],
    )
    monkeypatch.setenv("AUDIT_LOG_PATH", str(trader_log))
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_qqq_option_signal_runtime_acceptance.py",
            "--date",
            date,
            "--audit-dir",
            str(tmp_path / "runtime" / "audit"),
        ],
    )
    code = main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert code == 0
    assert payload["status"] == "PASS"


def test_runtime_acceptance_passes_with_legacy_entry_block_logs(tmp_path: Path, monkeypatch, capsys) -> None:
    date = "2026-04-09"
    audit_dir = tmp_path / "runtime" / "audit" / date
    strategy_file = audit_dir / "strategy-001.jsonl"
    _w(
        strategy_file,
        [
            {
                "ts": "2026-04-09T09:31:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_evaluated",
                "status": "entry_allowed",
            },
            {
                "ts": "2026-04-09T09:31:01-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "stock_signal_publish_ok",
                "status": "published",
            },
            {
                "ts": "2026-04-09T09:31:10-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "feature_callback_heartbeat",
                "status": "alive",
            },
        ],
    )
    plugin_log = tmp_path / "plugin_legacy.jsonl"
    _w(
        plugin_log,
        [
            {
                "event": "qqq_intraday_option_minute_heartbeat",
                "strategy_id": "qqq_intraday_option_v1_001",
                "signal_source_mode": "upstream_stock_signal",
            },
            {
                "event": "qqq_intraday_option_entry_block",
                "strategy_id": "qqq_intraday_option_v1_001",
                "signal_source_mode": "upstream_stock_signal",
                "gate_reason": "source_signal_missing",
            },
        ],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_qqq_option_signal_runtime_acceptance.py",
            "--date",
            date,
            "--audit-dir",
            str(tmp_path / "runtime" / "audit"),
            "--plugin-log-glob",
            str(plugin_log),
        ],
    )
    code = main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert code == 0
    assert payload["status"] == "PASS"


def test_runtime_acceptance_fails_when_stock_publish_failed_exists(tmp_path: Path, monkeypatch, capsys) -> None:
    date = "2026-04-09"
    audit_dir = tmp_path / "runtime" / "audit" / date
    strategy_file = audit_dir / "strategy-001.jsonl"
    _w(
        strategy_file,
        [
            {
                "ts": "2026-04-09T09:31:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_evaluated",
                "status": "entry_allowed",
            },
            {
                "ts": "2026-04-09T09:31:01-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "stock_signal_publish_failed",
                "status": "failed",
                "payload": {"error": "name 'continuity_ok' is not defined"},
            },
            {
                "ts": "2026-04-09T09:31:10-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "feature_callback_heartbeat",
                "status": "alive",
            },
        ],
    )
    plugin_log = tmp_path / "plugin_failed.jsonl"
    _w(
        plugin_log,
        [
            {
                "event": "qqq_intraday_option_minute_heartbeat",
                "strategy_id": "qqq_intraday_option_v1_001",
                "signal_source_mode": "upstream_stock_signal",
                "signal_plane_backend_config": "auto",
                "signal_plane_state": "healthy_redis",
                "signal_plane_redis_ok": True,
            },
            {
                "event": "qqq_intraday_option_downstream_result",
                "strategy_id": "qqq_intraday_option_v1_001",
                "result_code": "source_signal_missing",
            },
        ],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_qqq_option_signal_runtime_acceptance.py",
            "--date",
            date,
            "--audit-dir",
            str(tmp_path / "runtime" / "audit"),
            "--plugin-log-glob",
            str(plugin_log),
        ],
    )
    code = main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert code == 2
    assert payload["status"] == "FAIL"
    assert payload["checks"]["H9_no_stock_signal_publish_failures"] is False


def test_runtime_acceptance_fails_when_entry_allowed_has_no_publish_ok(tmp_path: Path, monkeypatch, capsys) -> None:
    date = "2026-04-09"
    audit_dir = tmp_path / "runtime" / "audit" / date
    strategy_file = audit_dir / "strategy-001.jsonl"
    _w(
        strategy_file,
        [
            {
                "ts": "2026-04-09T09:31:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "stage": "gate_evaluated",
                "status": "entry_allowed",
            },
            {
                "ts": "2026-04-09T09:31:10-04:00",
                "strategy_id": "qqq_intraday_option_v1_001",
                "stage": "feature_callback_heartbeat",
                "status": "alive",
            },
        ],
    )
    plugin_log = tmp_path / "plugin_missing_publish.jsonl"
    _w(
        plugin_log,
        [
            {
                "event": "qqq_intraday_option_minute_heartbeat",
                "strategy_id": "qqq_intraday_option_v1_001",
                "signal_source_mode": "upstream_stock_signal",
                "signal_plane_backend_config": "auto",
                "signal_plane_state": "healthy_redis",
                "signal_plane_redis_ok": True,
            },
            {
                "event": "qqq_intraday_option_downstream_result",
                "strategy_id": "qqq_intraday_option_v1_001",
                "result_code": "source_signal_missing",
            },
        ],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_qqq_option_signal_runtime_acceptance.py",
            "--date",
            date,
            "--audit-dir",
            str(tmp_path / "runtime" / "audit"),
            "--plugin-log-glob",
            str(plugin_log),
        ],
    )
    code = main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert code == 2
    assert payload["status"] == "FAIL"
    assert payload["checks"]["H3_stock_signal_published_for_entry_allowed"] is False
