import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.reset_qqqv1_daylock import reset_daylock


def test_reset_daylock_updates_trade_lock_fields(tmp_path):
    state_file = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    state_file.write_text(
        json.dumps(
            {
                "day": "2026-02-17",
                "is_traded": True,
                "success_count": 1,
                "pending_decision_id": "d-1",
                "exec_symbol": "TQQQ",
                "side": "LONG",
                "entry_px": 48.18,
                "stop_px": 47.05,
                "custom_keep_key": "keep_me",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = reset_daylock(state_file, day="2026-02-17", reason="unit_test")

    assert out["is_traded"] is False
    assert out["success_count"] == 0
    assert out["pending_decision_id"] is None
    assert out["exec_symbol"] is None
    assert out["side"] is None
    assert out["entry_px"] is None
    assert out["stop_px"] is None
    assert out["custom_keep_key"] == "keep_me"


def test_reset_daylock_creates_state_file_when_missing(tmp_path):
    state_file = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    out = reset_daylock(state_file, day="2026-02-17", reason="unit_test_create")

    assert state_file.exists()
    assert out["day"] == "2026-02-17"
    assert out["is_traded"] is False
    assert out["success_count"] == 0
