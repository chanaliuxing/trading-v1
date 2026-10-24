from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_replica_fixture_replay import build_fixture_report


def test_build_fixture_report_matches_fixture_to_actual_trace(tmp_path: Path) -> None:
    catalog = {
        "fixtures": {
            "gate_allowed_long": {
                "category": "gate",
                "events": [
                    {
                        "stage": "entry_gate_allowed",
                        "symbol": "QQQ",
                        "trace_id": "fixture:gate_allowed_long",
                        "reason": "entry_allowed",
                        "payload": {"side": "LONG", "qty": 8.0, "reason": "entry_allowed"},
                    }
                ],
            }
        }
    }
    actual = {
        "events": [
            {
                "stage": "entry_gate_allowed",
                "symbol": "QQQ",
                "trace_id": "fixture:gate_allowed_long",
                "reason": "entry_allowed",
                "payload": {"side": "LONG", "qty": 8.0, "reason": "entry_allowed"},
            }
        ]
    }
    catalog_path = tmp_path / "catalog.json"
    actual_path = tmp_path / "actual.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    actual_path.write_text(json.dumps(actual), encoding="utf-8")

    report = build_fixture_report(
        catalog_path=catalog_path,
        fixture_name="gate_allowed_long",
        actual_trace_path=actual_path,
        qty_tolerance=1.0,
    )
    assert report["fixture_category"] == "gate"
    assert report["comparison"]["ok"] is True


def test_build_fixture_report_flags_missing_fixture(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    actual_path = tmp_path / "actual.json"
    catalog_path.write_text(json.dumps({"fixtures": {}}), encoding="utf-8")
    actual_path.write_text(json.dumps({"events": []}), encoding="utf-8")

    try:
        build_fixture_report(
            catalog_path=catalog_path,
            fixture_name="missing_fixture",
            actual_trace_path=actual_path,
            qty_tolerance=1.0,
        )
    except KeyError as exc:
        assert "missing_fixture" in str(exc)
    else:
        raise AssertionError("expected KeyError")
