import json
from pathlib import Path

from tool.report.build_qqq_shadow_drift_report import build_qqq_shadow_drift_report


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_build_qqq_shadow_drift_report_holds_on_unresolved_diff(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    snapshot_root.mkdir()
    parity_root.mkdir()
    for day in ("20260419", "20260420", "20260421"):
        (snapshot_root / f"qqq_signal_snapshots_{day}.jsonl").write_text(
            json.dumps({"decision_id": f"d-{day}", "ts_et": f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:00-04:00"}) + "\n",
            encoding="utf-8",
        )
    _write_json(
        parity_root / "20260421.json",
        {
            "day": "20260421",
            "recommendation": "HOLD",
            "diff_total": 1,
            "match_ratio": 0.0,
            "legacy_count": 1,
            "kernel_count": 1,
            "rows": [
                {
                    "key": "decision:d1",
                    "matched": False,
                    "diffs": [{"field": "exec_symbol", "expected": "TQQQ", "actual": "SQQQ"}],
                }
            ],
        },
    )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root)

    assert report["d3_ready"] is True
    assert report["recommendation"] == "HOLD"
    assert "unresolved_diffs_present" in report["blockers"]
    assert report["unresolved_diff_count"] == 1
    assert report["blocking_unresolved_diff_count"] == 1
    assert report["needs_48h_attribution_count"] == 1
    assert report["signal_snapshot_count_by_day"] == {"20260419": 1, "20260420": 1, "20260421": 1}


def test_build_qqq_shadow_drift_report_passes_with_approved_whitelist(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    whitelist = tmp_path / "whitelist.json"
    snapshot_root.mkdir()
    parity_root.mkdir()
    for day in ("20260419", "20260420", "20260421"):
        (snapshot_root / f"qqq_signal_snapshots_{day}.jsonl").write_text(
            json.dumps({"decision_id": f"d-{day}", "ts_et": f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:00-04:00"}) + "\n",
            encoding="utf-8",
        )
    _write_json(
        parity_root / "20260421.json",
        {
            "day": "20260421",
            "recommendation": "HOLD",
            "diff_total": 1,
            "legacy_count": 1,
            "kernel_count": 1,
            "rows": [
                {
                    "key": "decision:d1",
                    "matched": False,
                    "diffs": [{"field": "decision_ts_delta_sec", "expected": 0.0, "actual": 0.000001}],
                }
            ],
        },
    )
    _write_json(
        whitelist,
        {"entries": [{"key": "decision:d1", "field": "decision_ts_delta_sec", "approved": True, "reason": "microsecond_ts_rounding"}]},
    )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root, whitelist_path=whitelist)

    assert report["d3_ready"] is True
    assert report["recommendation"] == "PASS"
    assert report["blockers"] == []
    assert report["unresolved_diff_count"] == 0
    assert report["whitelisted_diff_count"] == 1


def test_build_qqq_shadow_drift_report_holds_without_parity_reports(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    snapshot_root.mkdir()
    parity_root.mkdir()
    for day in ("20260419", "20260420", "20260421"):
        (snapshot_root / f"qqq_signal_snapshots_{day}.jsonl").write_text(
            json.dumps({"decision_id": f"d-{day}", "ts_et": f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:00-04:00"}) + "\n",
            encoding="utf-8",
        )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root)

    assert report["d3_ready"] is True
    assert report["recommendation"] == "HOLD"
    assert report["parity_report_count"] == 0
    assert report["blockers"] == ["no_parity_report_yet"]


def test_build_qqq_shadow_drift_report_holds_on_empty_parity_input(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    snapshot_root.mkdir()
    parity_root.mkdir()
    _write_json(
        parity_root / "20260421_postclose_parity_legacy_on.json",
        {
            "day": "20260421",
            "recommendation": "PASS",
            "diff_total": 0,
            "match_ratio": 1.0,
            "legacy_count": 0,
            "kernel_count": 0,
            "rows": [],
        },
    )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root)

    assert report["recommendation"] == "HOLD"
    assert report["empty_parity_report_count"] == 1
    assert "empty_parity_report_input" in report["blockers"]


def test_build_qqq_shadow_drift_report_uses_min_match_ratio(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    snapshot_root.mkdir()
    parity_root.mkdir()
    for idx, ratio in enumerate((1.0, 0.75), start=1):
        _write_json(
            parity_root / f"2026042{idx}_postclose_parity_legacy_on.json",
            {
                "day": f"2026042{idx}",
                "recommendation": "PASS",
                "diff_total": 0,
                "match_ratio": ratio,
                "legacy_count": 4,
                "kernel_count": 4,
                "rows": [],
            },
        )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root)

    assert report["recommendation"] == "CONTINUE_SHADOW"
    assert report["min_match_ratio"] == 0.75


def test_build_qqq_shadow_drift_report_flags_presence_only_alignment_risk(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    snapshot_root.mkdir()
    parity_root.mkdir()
    for day in ("20260419", "20260420", "20260421"):
        (snapshot_root / f"qqq_signal_snapshots_{day}.jsonl").write_text(
            json.dumps({"decision_id": f"d-{day}", "ts_et": f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:00-04:00"}) + "\n",
            encoding="utf-8",
        )
    _write_json(
        parity_root / "20260421_postclose_parity_legacy_on.json",
        {
            "day": "20260421",
            "recommendation": "HOLD",
            "diff_total": 120,
            "match_ratio": 0.0,
            "legacy_count": 20,
            "kernel_count": 500,
            "rows": [
                {
                    "key": f"decision:d{i}",
                    "matched": False,
                    "diffs": [{"field": "__presence__", "expected": True, "actual": False}],
                }
                for i in range(120)
            ],
        },
    )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root)

    assert report["presence_only_unresolved_count"] == 120
    assert report["presence_only_unresolved_ratio"] == 1.0
    assert report["suspected_key_alignment_mismatch"] is True


def test_build_qqq_shadow_drift_report_ignores_non_blocking_semantic_diffs_for_hold(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    parity_root = tmp_path / "parity"
    snapshot_root.mkdir()
    parity_root.mkdir()
    for day in ("20260419", "20260420", "20260421"):
        (snapshot_root / f"qqq_signal_snapshots_{day}.jsonl").write_text(
            json.dumps({"decision_id": f"d-{day}", "ts_et": f"{day[:4]}-{day[4:6]}-{day[6:]}T10:00:00-04:00"}) + "\n",
            encoding="utf-8",
        )
    _write_json(
        parity_root / "20260421_postclose_parity_legacy_on.json",
        {
            "day": "20260421",
            "recommendation": "PASS",
            "diff_total": 0,
            "match_ratio": 1.0,
            "legacy_count": 1,
            "kernel_count": 1,
            "rows": [
                {
                    "key": "minute:202604210930",
                    "matched": False,
                    "blocking": False,
                    "diff_classification": "semantic_equivalent",
                    "diffs": [
                        {
                            "field": "block_reason",
                            "expected": "amp_not_locked",
                            "actual": "sign_mismatch",
                            "classification": "semantic_equivalent",
                        }
                    ],
                }
            ],
        },
    )

    report = build_qqq_shadow_drift_report(snapshot_root=snapshot_root, parity_root=parity_root)

    assert report["recommendation"] == "PASS"
    assert report["unresolved_diff_count"] == 1
    assert report["blocking_unresolved_diff_count"] == 0
    assert report["unresolved_diff_count_by_classification"] == {"semantic_equivalent": 1}
    assert report["blockers"] == []
