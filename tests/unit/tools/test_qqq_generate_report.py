from tools.qqq_generate_report import _in_time_range, _render_minute_table


def test_in_time_range_uses_from_to_window():
    day = "2026-02-09"
    assert _in_time_range("2026-02-09T09:30:00-05:00", day, "09:30", "16:00") is True
    assert _in_time_range("2026-02-09T16:00:59-05:00", day, "09:30", "16:00") is True
    assert _in_time_range("2026-02-09T09:29:59-05:00", day, "09:30", "16:00") is False
    assert _in_time_range("2026-02-09T16:01:00-05:00", day, "09:30", "16:00") is False


def test_minute_table_contains_gate_matrix_pass_fail():
    rows = [
        {
            "minute": "2026-02-09T10:00:00-05:00",
            "hb": {
                "is_traded": False,
                "amp_locked": True,
                "data_age_qqq_sec": 1.2,
                "data_age_qqew_sec": 1.1,
                "data_age_add_sec": 1.1,
            },
            "gate": {
                "reason": "strength_not_ok",
                "amp_locked": True,
                "long_ok": True,
                "short_ok": False,
                "qqq_last": 500.0,
                "qqq_vwap": 499.0,
                "qqew_last": 104.0,
                "qqew_vwap": 103.0,
                "add_now": 2100.0,
                "add_ago": 1800.0,
                "velocity": 300.0,
                "level": 2100.0,
                "add_velocity_threshold": 120.0,
                "add_level_threshold": 800.0,
                "strength_ok": False,
                "atr": 0.12,
                "exec_symbol": "TQQQ",
            },
            "missing": None,
            "orders": [],
            "fills": [],
            "events": [],
        }
    ]
    lines = _render_minute_table(rows)
    text = "\n".join(lines)
    assert "Gate Matrix" in lines[0]
    assert "data_live=PASS" in text
    assert "qqq_trend=PASS" in text
    assert "qqew_trend=PASS" in text
    assert "strength=FAIL" in text
