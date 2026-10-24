import json
from pathlib import Path

from tool.replay.run_option_launch_go_no_go_gate import main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_run_option_launch_go_no_go_gate_writes_report(tmp_path: Path, monkeypatch) -> None:
    platform = tmp_path / "platform.json"
    rollout = tmp_path / "rollout.json"
    active = tmp_path / "active.json"
    qqq = tmp_path / "qqq.json"
    out = tmp_path / "gate.json"

    _write_json(platform, {"platform_admission_blocked": False, "recommendation": "PLATFORM_ADMISSION_REVIEW_ALLOWED"})
    _write_json(rollout, {"recommendation": "ROLLOUT_CONTRACT_READY"})
    _write_json(
        active,
        {
            "recommendation": "ACTIVE_STRATEGIES_EVIDENCE_READY",
            "strategies": [
                {"strategy_id": "qqq_intraday_option_v1_001", "reasons": []},
                {"strategy_id": "spy_1dte_ma10_reject_0324_001", "reasons": []},
            ],
        },
    )
    _write_json(
        qqq,
        {
            "recommendation": "READY",
            "cutover_still_blocked": False,
            "post_instrumentation_day_count": 3,
            "post_instrumentation_pass_days": 3,
            "post_instrumentation_fail_days": 0,
        },
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_option_launch_go_no_go_gate.py",
            "--platform-report",
            str(platform),
            "--rollout-report",
            str(rollout),
            "--active-evidence-report",
            str(active),
            "--qqq-cutover-report",
            str(qqq),
            "--output",
            str(out),
        ],
    )
    main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["recommendation"] == "GO"
