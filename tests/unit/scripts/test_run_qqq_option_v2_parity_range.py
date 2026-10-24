import json
from pathlib import Path

from scripts.run_qqq_option_v2_parity_range import _iter_days, _summarize


def test_iter_days_inclusive():
    assert _iter_days("2026-03-24", "2026-03-26") == [
        "2026-03-24",
        "2026-03-25",
        "2026-03-26",
    ]


def test_summarize_counts_matched_days():
    payload = _summarize(
        [
            {"day": "2026-03-24", "matched": True, "old_count": 2, "new_count": 2},
            {"day": "2026-03-25", "matched": False, "old_count": 1, "new_count": 0},
        ]
    )
    assert payload["days_total"] == 2
    assert payload["matched_day_count"] == 1
    assert payload["mismatched_day_count"] == 1
    assert payload["day_parity_rate"] == 0.5
    assert payload["count_parity_ok"] is False


def test_range_script_writes_output(monkeypatch, tmp_path: Path):
    from scripts import run_qqq_option_v2_parity_range as script

    monkeypatch.setattr(
        script,
        "run_clickhouse_day_parity_scenario_sync",
        lambda scenario: {
            "matched": True,
            "old_count": 1,
            "new_count": 1,
            "diff": {"matched": True, "diffs": []},
        },
    )

    out = tmp_path / "range.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_qqq_option_v2_parity_range.py",
            "--start",
            "2026-03-24",
            "--end",
            "2026-03-25",
            "--out",
            str(out),
        ],
    )
    script.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["days_total"] == 2
    assert payload["summary"]["matched_day_count"] == 2
    assert payload["summary"]["count_parity_ok"] is True
