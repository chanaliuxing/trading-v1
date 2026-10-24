import asyncio
import json
from subprocess import CompletedProcess

import pytest

from scripts.ai_trader import control_event_consumer as cec


@pytest.mark.asyncio
async def test_backfill_gap_scanning(monkeypatch):
    """
    Validate that _execute_backfill scans missing dates and only issues chunked commands
    for the gaps, not the full window.
    """
    issued_cmds = []

    def fake_run(cmd, capture_output=False, text=False, timeout=None, cwd=None):
        # Gap scan call
        if "check_active_completeness.py" in cmd[1]:
            payload = {
                "by_date": [
                    {"date": "2025-11-10", "coverage": {"AAPL": 0.5, "TSLA": 1.0}},
                    {"date": "2025-11-11", "coverage": {"AAPL": 0.3, "TSLA": 1.0}},
                    {"date": "2025-11-20", "coverage": {"AAPL": 0.6}},
                ]
            }
            return CompletedProcess(cmd, 0, json.dumps(payload), "")
        # Actual backfill calls
        if "tool/load_hist_multi.py" in cmd[1]:
            issued_cmds.append(cmd)
            return CompletedProcess(cmd, 0, "OK", "")
        return CompletedProcess(cmd, 0, "", "")

    async def fake_enqueue_ml_job(params, job_id):
        return {"status": "ok"}

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(cec, "subprocess", type("S", (), {"run": fake_run}))
    monkeypatch.setattr(cec, "_enqueue_ml_job", fake_enqueue_ml_job)
    monkeypatch.setattr(cec, "asyncio", type("A", (), {"sleep": fake_sleep}))

    res = await cec._execute_backfill(
        {
            "symbols": ["AAPL", "TSLA"],
            "days": 15,
            "bar_size": "1m",
            "chunk_days": 3,
            "force_full_window": False,
        },
        "gap-test",
    )

    assert res["status"] == "ok"
    assert len(issued_cmds) == 1
    cmd = issued_cmds[0]
    # Expected days: today - earliest_gap + 1
    import datetime as _dt
    today = _dt.date.today()
    earliest_gap = _dt.date.fromisoformat("2025-11-10")
    expected_days = max(15, (today - earliest_gap).days + 1)
    # Extract days value from command list
    days_idx = cmd.index("--days") + 1
    actual_days = int(cmd[days_idx])
    assert actual_days == expected_days
