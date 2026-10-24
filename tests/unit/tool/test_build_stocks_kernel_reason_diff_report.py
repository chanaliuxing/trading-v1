from tool.report.build_stocks_kernel_reason_diff_report import build_reason_diff_report


def test_build_reason_diff_report_extracts_only_mismatches():
    payload = build_reason_diff_report(
        parity_report={
            "comparisons": [
                {"index": 0, "matched": True, "field_diffs": []},
                {
                    "index": 1,
                    "matched": False,
                    "field_diffs": [{"field": "signal_side", "expected": "LONG", "actual": "SHORT"}],
                    "expected": {"signal_side": "LONG"},
                    "actual": {"signal_side": "SHORT"},
                },
            ]
        }
    )

    assert payload["mismatch_count"] == 1
    assert payload["rows"][0]["field_diffs"][0]["field"] == "signal_side"
