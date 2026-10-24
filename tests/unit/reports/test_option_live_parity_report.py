import json
from pathlib import Path

from services.reports.option_live_parity_report import generate_option_live_parity_report


def test_generate_option_live_parity_report_matches_replay_and_shadow(tmp_path: Path):
    strategy_id = "qqq_1dte_ma10_reject_0324_001"
    day = "2026-03-25"

    snapshot_dir = tmp_path / "snapshots" / strategy_id / day
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "s1.json").write_text(
        json.dumps(
            {
                "snapshot_id": "s1",
                "parity_profile_id": "qqq_option_ma_ext_v1",
                "bar_close_ts": "2026-03-25T11:11:00-04:00",
                "signal": {"signal_kind": "trend_continuation_put"},
            }
        ),
        encoding="utf-8",
    )

    audit_dir = tmp_path / "audit" / day
    audit_dir.mkdir(parents=True)
    (audit_dir / "strategy-11.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "strategy_id": strategy_id,
                        "stage": "entry_snapshot_written",
                        "ts": "2026-03-25T11:11:00-04:00",
                    }
                ),
                json.dumps(
                    {
                        "strategy_id": strategy_id,
                        "stage": "entry_shadow_mode_skipped",
                        "ts": "2026-03-25T11:11:00-04:00",
                    }
                ),
                json.dumps(
                    {
                        "strategy_id": strategy_id,
                        "stage": "entry_snapshot_duplicate_blocked",
                        "ts": "2026-03-25T11:11:00-04:00",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "trades.csv").write_text(
        "entry_ts,signal_kind\n"
        "2026-03-25T11:11:00-04:00,trend_continuation_put\n",
        encoding="utf-8",
    )

    report = generate_option_live_parity_report(
        strategy_id=strategy_id,
        report_date=day,
        snapshot_root=tmp_path / "snapshots",
        audit_root=tmp_path / "audit",
        replay_dir=replay_dir,
    )

    payload = report.to_dict()
    assert payload["strategy_id"] == strategy_id
    assert payload["parity_profile_id"] == "qqq_option_ma_ext_v1"
    assert payload["snapshots_total"] == 1
    assert payload["replay_expected_entries"] == 1
    assert payload["live_shadow_entries"] == 1
    assert payload["matched_entries"] == 1
    assert payload["missing_live_entries"] == 0
    assert payload["extra_live_entries"] == 0
    assert payload["duplicate_blocked_entries"] == 1
    assert payload["shadow_skipped_entries"] == 1
    assert payload["trade_parity"] == 1.0
    assert payload["no_trade_parity_ok"] is True
