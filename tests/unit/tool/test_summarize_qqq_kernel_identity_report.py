from tool.replay.summarize_qqq_kernel_identity_report import summarize


def test_summarize_groups_reason_pairs_and_hours() -> None:
    payload = {
        "ok": True,
        "day": "2026-04-08",
        "rows": 10,
        "mismatch_count": 3,
        "parity": {"allow_match_ratio": 0.7},
        "mismatches": [
            {
                "ts_iso": "2026-04-08T09:45:00-04:00",
                "legacy": {"reason": "anti_chase_blocked"},
                "kernel": {"reason": "direction_allowed"},
            },
            {
                "ts_iso": "2026-04-08T09:46:00-04:00",
                "legacy": {"reason": "anti_chase_blocked"},
                "kernel": {"reason": "direction_allowed"},
            },
            {
                "ts_iso": "2026-04-08T10:01:00-04:00",
                "legacy": {"reason": "sign_mismatch"},
                "kernel": {"reason": "fast_drift_not_ready"},
            },
        ],
    }
    out = summarize(payload)
    assert out["mismatch_ratio"] == 0.3
    assert out["top_reason_pairs"][0]["pair"] == "anti_chase_blocked -> direction_allowed"
    assert out["top_reason_pairs"][0]["count"] == 2
    assert out["top_reason_pairs"][0]["hours"]["09:00"] == 2
