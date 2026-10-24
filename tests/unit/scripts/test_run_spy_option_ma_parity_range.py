import json
from pathlib import Path

from scripts.run_spy_option_ma_parity_range import (
    _iter_days,
    _load_strategy_extras,
    _normalize_extras_for_harness,
    _summarize,
)


def test_iter_days_inclusive() -> None:
    assert _iter_days("2026-03-24", "2026-03-26") == ["2026-03-24", "2026-03-25", "2026-03-26"]


def test_summarize_counts_matched_days() -> None:
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


def test_range_script_writes_output(monkeypatch, tmp_path: Path) -> None:
    from scripts import run_spy_option_ma_parity_range as script

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
            "run_spy_option_ma_parity_range.py",
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
    assert payload["strategy_id"] == "spy_1dte_ma10_reject_0324_001"


def test_load_strategy_extras_from_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        """
strategies:
  - id: spy_1dte_ma10_reject_0324_001
    params:
      extras:
        underlying: SPY
        risk_symbol: OPT:SPY:1DTE
        max_trades_per_day: 4
""".strip()
        + "\n",
        encoding="utf-8",
    )
    extras = _load_strategy_extras(cfg, "spy_1dte_ma10_reject_0324_001")
    assert extras["underlying"] == "SPY"
    assert extras["risk_symbol"] == "OPT:SPY:1DTE"
    assert extras["max_trades_per_day"] == 4


def test_normalize_extras_for_harness_includes_harness_ids() -> None:
    payload = _normalize_extras_for_harness(
        {
            "shared_cap_group_strategy_ids": [
                "spy_1dte_ma10_reject_0324_001",
                "spy_1dte_ma10_reject_0324_pure_001",
            ]
        }
    )
    members = payload["shared_cap_group_strategy_ids"]
    assert "spy_1dte_ma10_reject_v2_harness" in members
    assert "spy_1dte_ma10_reject_legacy_harness" in members
