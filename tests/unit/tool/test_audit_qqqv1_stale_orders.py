import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.audit_qqqv1_stale_orders import audit_local_state


def _state_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_audit_local_state_warns_on_pending_today(tmp_path, monkeypatch):
    strategy_id = "qqq_intraday_001"
    today = "2026-02-14"
    monkeypatch.setattr("tool.audit_qqqv1_stale_orders._today_str_et", lambda: today)

    state_path = tmp_path / f"qqq_intraday_{strategy_id}.json"
    _state_file(
        state_path,
        {
            "day": today,
            "updated_at": datetime(2026, 2, 14, 9, 31, 0).isoformat(),
            "reason": "unit_test",
            "is_traded": True,
            "pending_decision_id": "d-1",
            "exec_symbol": "TQQQ",
            "side": "LONG",
            "stop_px": 99.5,
        },
    )

    findings = audit_local_state(strategy_id=strategy_id, state_dir=tmp_path)
    levels = [f.level for f in findings]
    messages = [f.message for f in findings]

    assert "OK" in levels
    assert "WARN" in levels
    assert "State indicates a pending decision today (may block new entry)" in messages
    assert "State indicates an active trade today (ensure broker + DB are clean before restart)" in messages


def test_audit_local_state_marks_previous_day_as_non_restored(tmp_path, monkeypatch):
    strategy_id = "qqq_intraday_001"
    monkeypatch.setattr("tool.audit_qqqv1_stale_orders._today_str_et", lambda: "2026-02-14")

    state_path = tmp_path / f"qqq_intraday_{strategy_id}.json"
    _state_file(
        state_path,
        {
            "day": "2026-02-13",
            "updated_at": datetime(2026, 2, 13, 16, 0, 0).isoformat(),
            "reason": "shutdown",
            "is_traded": True,
            "pending_decision_id": None,
            "exec_symbol": None,
            "side": None,
            "stop_px": None,
        },
    )

    findings = audit_local_state(strategy_id=strategy_id, state_dir=tmp_path)
    messages = [f.message for f in findings]

    assert "State file is from a previous day (will not be restored today)" in messages

