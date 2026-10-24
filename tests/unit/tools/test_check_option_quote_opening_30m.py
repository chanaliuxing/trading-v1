from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.check_option_quote_opening_30m import build_opening_30m_report, main

ET = ZoneInfo("America/New_York")


def _mk_row(ts: datetime, *, stage: str, strategy_id: str, reason: str | None = None, payload: dict | None = None) -> dict:
    return {
        "ts": ts.isoformat(),
        "domain": "strategy",
        "stage": stage,
        "strategy_id": strategy_id,
        "reason": reason,
        "payload": dict(payload or {}),
    }


def test_build_opening_30m_report_go_when_healthy():
    day = date(2026, 4, 6)
    sid = "qqq_0dte_opt_live_001"
    start = datetime(2026, 4, 6, 9, 30, tzinfo=ET)
    rows = [
        _mk_row(
            start + timedelta(minutes=i),
            stage="minute_heartbeat",
            strategy_id=sid,
            payload={"quote_source": "memory", "quote_age_sec": 9.0},
        )
        for i in range(30)
    ]
    report = build_opening_30m_report(
        rows=rows,
        day=day,
        strategy_id=sid,
        window_min=30,
        max_quote_age_sec=120.0,
        max_clickhouse_ratio=0.35,
        min_heartbeat_ratio=0.8,
    )
    assert report["go"] is True
    assert report["checks"]["freshness_ok"] is True
    assert report["checks"]["fallback_ratio_ok"] is True
    assert report["heartbeats_total"] == 30


def test_build_opening_30m_report_no_go_when_clickhouse_ratio_and_pause_high():
    day = date(2026, 4, 6)
    sid = "qqq_0dte_opt_live_001"
    start = datetime(2026, 4, 6, 9, 30, tzinfo=ET)
    rows = []
    for i in range(10):
        rows.append(
            _mk_row(
                start + timedelta(minutes=i),
                stage="minute_heartbeat",
                strategy_id=sid,
                payload={"quote_source": "clickhouse", "quote_age_sec": 10.0},
            )
        )
    rows.append(
        _mk_row(
            start + timedelta(minutes=5),
            stage="gate_blocked",
            strategy_id=sid,
            reason="QUOTE_HEARTBEAT_PAUSED",
        )
    )
    report = build_opening_30m_report(
        rows=rows,
        day=day,
        strategy_id=sid,
        window_min=30,
        max_quote_age_sec=120.0,
        max_clickhouse_ratio=0.35,
        min_heartbeat_ratio=0.8,
    )
    assert report["go"] is False
    assert report["checks"]["coverage_ok"] is False
    assert report["checks"]["fallback_ratio_ok"] is False


def test_script_main_writes_report(monkeypatch, tmp_path: Path):
    day = date(2026, 4, 6)
    sid = "qqq_0dte_opt_live_001"
    day_dir = tmp_path / "audit" / day.isoformat()
    day_dir.mkdir(parents=True)
    start = datetime(2026, 4, 6, 9, 30, tzinfo=ET)
    rows = [
        _mk_row(
            start + timedelta(minutes=i),
            stage="minute_heartbeat",
            strategy_id=sid,
            payload={"quote_source": "memory", "quote_age_sec": 5.0},
        )
        for i in range(30)
    ]
    (day_dir / "strategy-09.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    out = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_option_quote_opening_30m.py",
            "--date",
            day.isoformat(),
            "--strategy-id",
            sid,
            "--audit-root",
            str(tmp_path / "audit"),
            "--out",
            str(out),
        ],
    )
    main()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["strategy_id"] == sid
    assert payload["go"] is True
