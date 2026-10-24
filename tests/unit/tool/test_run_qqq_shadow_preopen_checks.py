from __future__ import annotations

from pathlib import Path
import json

import tool.ops.run_qqq_shadow_preopen_checks as preopen_checks


def test_preopen_checks_skip_non_trading_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(preopen_checks, "is_trading_day", lambda d: False)

    payload = preopen_checks.build_preopen_checks(
        day="20260425",
        config_path=tmp_path / "config.yml",
        trace_root=tmp_path / "reports/qqq_signal_snapshots",
        report_root=tmp_path / "reports/qqq_kernel_shadow_parity",
    )

    assert payload["recommendation"] == "PASS"
    assert payload["blockers"] == []
    assert payload["market_calendar"]["trading_day"] is False


def test_preopen_checks_require_live_vs_shadow_parity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(preopen_checks, "is_trading_day", lambda d: True)
    config_path = tmp_path / "config.yml"
    config_path.write_text("strategies: []\n", encoding="utf-8")
    trace_root = tmp_path / "reports/qqq_signal_snapshots"
    report_root = tmp_path / "reports/qqq_kernel_shadow_parity"
    trace_root.mkdir(parents=True, exist_ok=True)

    live_row = {"strategy_id": "qqq_intraday_001", "minute": "2026-04-28 09:40", "side": "LONG", "exec_symbol": "TQQQ"}
    on_row = {"strategy_id": "qqq_kernel_shadow_legacy_on_001", "minute": "2026-04-28 09:40", "side": "LONG", "exec_symbol": "TQQQ"}
    off_row = {"strategy_id": "qqq_kernel_shadow_legacy_off_001", "minute": "2026-04-28 09:40", "side": "LONG", "exec_symbol": "TQQQ"}
    (trace_root / "qqq_signal_snapshots_20260428.jsonl").write_text(json.dumps(live_row) + "\n", encoding="utf-8")
    (trace_root / "qqq_signal_snapshots_20260428_qqq_kernel_shadow_legacy_on_001.jsonl").write_text(json.dumps(on_row) + "\n", encoding="utf-8")
    (trace_root / "qqq_signal_snapshots_20260428_qqq_kernel_shadow_legacy_off_001.jsonl").write_text(json.dumps(off_row) + "\n", encoding="utf-8")

    monkeypatch.setattr(preopen_checks, "build_readiness_report", lambda **kwargs: {"ok": True, "failed_checks": []})
    monkeypatch.setattr(preopen_checks, "_config_status", lambda path: {
        "qqq_kernel_shadow_legacy_on_001": "true",
        "qqq_kernel_shadow_legacy_off_001": "true",
    })
    monkeypatch.setattr(preopen_checks, "build_postclose_report", lambda **kwargs: {"recommendation": "PASS", "blockers": []})

    payload = preopen_checks.build_preopen_checks(
        day="20260428",
        config_path=config_path,
        trace_root=trace_root,
        report_root=report_root,
    )

    assert payload["recommendation"] == "PASS"
    assert payload["blockers"] == []
    assert payload["parity_smoke"]["ok"] is True
    assert payload["parity_smoke"]["pairs"]["live_vs_legacy_on"]["recommendation"] == "PASS"
    assert payload["parity_smoke"]["pairs"]["live_vs_legacy_off"]["recommendation"] == "PASS"
