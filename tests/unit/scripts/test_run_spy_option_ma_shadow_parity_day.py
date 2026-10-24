import json
from pathlib import Path

from scripts.run_spy_option_ma_shadow_parity_day import _next_day, main


def test_next_day() -> None:
    assert _next_day("2026-03-25") == "2026-03-26"


def test_shadow_parity_day_script_runs_replay_and_writes_report(monkeypatch, tmp_path: Path) -> None:
    calls = {}

    def _fake_run(cmd, cwd=None, check=None):
        calls["cmd"] = cmd
        replay_dir = Path(cmd[cmd.index("--out-dir") + 1])
        replay_dir.mkdir(parents=True, exist_ok=True)
        (replay_dir / "trades.csv").write_text(
            "entry_ts,signal_kind\n2026-03-25T11:11:00-04:00,trend_continuation_put\n",
            encoding="utf-8",
        )
        return None

    def _fake_report(**kwargs):
        class _Report:
            def to_dict(self):
                return {
                    "strategy_id": kwargs["strategy_id"],
                    "report_date": kwargs["report_date"],
                    "trade_parity": 1.0,
                    "matched_entries": 1,
                }

        return _Report()

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(
        "scripts.run_spy_option_ma_shadow_parity_day.generate_option_live_parity_report",
        _fake_report,
    )
    out = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_spy_option_ma_shadow_parity_day.py",
            "--date",
            "2026-03-25",
            "--strategy-id",
            "spy_1dte_ma10_reject_0324_001",
            "--snapshot-root",
            str(tmp_path / "snapshots"),
            "--audit-root",
            str(tmp_path / "audit"),
            "--replay-root",
            str(tmp_path / "replay"),
            "--out",
            str(out),
        ],
    )
    main()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["trade_parity"] == 1.0
    assert payload["matched_entries"] == 1
    assert "--start" in calls["cmd"]
    assert "--end" in calls["cmd"]

