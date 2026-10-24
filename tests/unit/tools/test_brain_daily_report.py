
import json
import pytest
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path
from tools.brain_daily_report import _build_report

@pytest.fixture
def mock_run_files(tmp_path: Path):
    """
    Creates a dummy run directory with ml_events.jsonl and ml_actions.jsonl
    for yesterday's date.
    """
    yesterday = date.today() - timedelta(days=1)
    run_dir = tmp_path / yesterday.isoformat()
    run_dir.mkdir(parents=True)

    # Create ml_events.jsonl with two relevant jobs
    events_content = [
        # An older, irrelevant job
        {"event": "ml.job", "status": "started", "job_type": "alpha_loop", "job_id": "job0", "ts": (datetime.now() - timedelta(hours=3)).isoformat()},
        # The "previous" job
        {"event": "ml.job", "status": "completed", "job_type": "alpha_loop", "job_id": "job1", "ts": (datetime.now() - timedelta(hours=2)).isoformat(), "metrics": {"sharpe": 1.0, "ic": 0.1}},
        # The "last" job
        {"event": "ml.job", "status": "completed", "job_type": "hpo_cli", "job_id": "job2", "ts": (datetime.now() - timedelta(hours=1)).isoformat(), "metrics": {"oos_avg_sharpe": 1.5, "oos_avg_ic": 0.15}},
    ]
    (run_dir / "ml_events.jsonl").write_text("\n".join(json.dumps(e) for e in events_content))

    # Create ml_actions.jsonl with one fix_config action
    yesterday_ts = datetime.now() - timedelta(days=1)
    actions_content = [
        {"action": "queue_job", "params": {}, "ts": yesterday_ts.isoformat(), "executed_at": yesterday_ts.isoformat()},
        {"action": "fix_config", "params": {"path": "ml_policy.json", "key": "label.horizon_sec", "value": 120}, "job_id": "job2", "ts": yesterday_ts.isoformat(), "executed_at": yesterday_ts.isoformat()},
    ]
    (run_dir / "ml_actions.jsonl").write_text("\n".join(json.dumps(a) for a in actions_content))
    
    return tmp_path

def test_brain_daily_report_summarises_last_two_jobs_and_fix_config(mock_run_files):
    """
    Tests that _build_report correctly summarizes the last two alpha jobs
    and counts the fix_config actions from mock log files.
    """
    # 1. Create dummy args pointing to the mock runs directory
    args = argparse.Namespace(
        runs_dir=str(mock_run_files),
        days=2 # Look at today and yesterday
    )

    # 2. Call the function to build the report
    report = _build_report(args)

    # 3. Assert the report content
    
    # Check job metrics
    assert report["jobs"]["last"]["job_id"] == "job2"
    assert report["jobs"]["last"]["sharpe"] == 1.5
    assert report["jobs"]["last"]["ic"] == 0.15

    assert report["jobs"]["prev"]["job_id"] == "job1"
    assert report["jobs"]["prev"]["sharpe"] == 1.0
    assert report["jobs"]["prev"]["ic"] == 0.1

    # Check deltas
    assert report["jobs"]["delta"]["delta_sharpe"] == pytest.approx(0.5)
    assert report["jobs"]["delta"]["delta_ic"] == pytest.approx(0.05)

    # Check fix_config actions
    assert len(report["fix_config_actions"]) == 1
    action = report["fix_config_actions"][0]
    assert action["path"] == "ml_policy.json"
    assert action["key"] == "label.horizon_sec"
    assert action["value"] == 120

    # Check summary text
    summary = report["summary_text"]
    assert "Last job: hpo_cli id=job2" in summary
    assert "Sharpe=1.5" in summary
    assert "IC=0.15" in summary
    assert "Prev job: alpha_loop id=job1" in summary
    assert "Sharpe=1.0" in summary
    assert "IC=0.1" in summary
    assert "ΔSharpe=" in summary
    assert "ΔIC=" in summary
    assert "fix_config actions on" in summary
    assert "1 change(s)" in summary
