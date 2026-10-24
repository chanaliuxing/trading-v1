import json
from pathlib import Path

from tool.replay.run_option_active_strategy_evidence_status import main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_run_option_active_strategy_evidence_status_writes_report(tmp_path: Path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    platform = tmp_path / "platform.json"
    rollout = tmp_path / "rollout.json"
    out = tmp_path / "active_evidence.json"

    _write_json(platform, {"platform_admission_blocked": False, "recommendation": "PLATFORM_ADMISSION_REVIEW_ALLOWED"})
    _write_json(
        rollout,
        {
            "recommendation": "ROLLOUT_CONTRACT_READY",
            "strategies": [
                {
                    "strategy_id": "qqq_intraday_option_v1_001",
                    "status": "active",
                    "rollout_contract_pass": True,
                    "issues": [],
                }
            ],
        },
    )
    _write_json(
        state_root / "qqq_state_shadow_acceptance" / "qqq_state_shadow_cutover_recommendation.json",
        {"recommendation": "READY", "cutover_still_blocked": False},
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_option_active_strategy_evidence_status.py",
            "--state-root",
            str(state_root),
            "--platform-report",
            str(platform),
            "--rollout-report",
            str(rollout),
            "--output",
            str(out),
        ],
    )
    main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["active_strategy_count"] == 1
    assert payload["blocked_strategy_count"] == 0
