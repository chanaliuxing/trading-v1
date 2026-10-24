from __future__ import annotations

from pathlib import Path

import pandas as pd

from services.option_replay.core.calibration import (
    CalibrationConfig,
    build_calibration_report,
    build_replay_fill_comparison_df,
    load_live_fills_csv,
    match_live_to_replay_fills,
)


def test_load_live_fills_csv_requires_minimum_columns(tmp_path: Path):
    path = tmp_path / "live.csv"
    pd.DataFrame({"fill_ts": ["2026-03-24 10:00:00"]}).to_csv(path, index=False)
    try:
        load_live_fills_csv(path)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "missing columns" in str(exc)


def test_match_live_to_replay_fills_matches_within_window():
    replay = pd.DataFrame(
        [
            {
                "fill_ts": pd.Timestamp("2026-03-24 10:01:00"),
                "side": "CALL",
                "expiry": "2026-03-25",
                "strike": 500.0,
                "qty": 1,
                "fill_price": 2.15,
                "order_id": "entry_1",
                "fill_reason": "ENTRY_FILL",
            }
        ]
    )
    live = pd.DataFrame(
        [
            {
                "fill_ts": pd.Timestamp("2026-03-24 10:01:30"),
                "side": "CALL",
                "expiry": "2026-03-25",
                "strike": 500.0,
                "qty": 1,
                "fill_price": 2.20,
            }
        ]
    )
    matches = match_live_to_replay_fills(replay, live, CalibrationConfig(match_window_min=2))
    assert len(matches) == 1
    assert bool(matches.iloc[0]["matched"]) is True
    assert abs(float(matches.iloc[0]["price_diff"]) - (-0.05)) < 1e-9


def test_build_replay_fill_comparison_df_normalizes_result():
    result = {
        "fills": pd.DataFrame(
            [
                {
                    "ts": "2026-03-24T10:01:00",
                    "side": "CALL",
                    "expiry": "2026-03-25",
                    "strike": 500.0,
                    "qty": 1,
                    "fill_price": 2.15,
                    "order_id": "entry_1",
                    "fill_reason": "ENTRY_FILL",
                }
            ]
        )
    }
    out = build_replay_fill_comparison_df(result)
    assert list(out.columns) == ["fill_ts", "side", "expiry", "strike", "qty", "fill_price", "order_id", "fill_reason"]
    assert len(out) == 1


def test_build_calibration_report_summarizes_matches():
    matches = pd.DataFrame(
        [
            {"matched": True, "time_diff_sec": 30.0, "price_diff": -0.05, "qty_diff": 0},
            {"matched": False, "time_diff_sec": pd.NA, "price_diff": pd.NA, "qty_diff": pd.NA},
        ]
    )
    report = build_calibration_report(matches)
    assert report["live_rows"] == 2
    assert report["matched_rows"] == 1
    assert abs(report["match_rate"] - 0.5) < 1e-9
