from libs.healer.data_healer_v2 import DataHealerV2


def test_coverage_check_handles_empty():
    healer = DataHealerV2(dry_run=True)
    missing = healer._check_label_coverage([], [30], 1, min_rows_per_symbol=1000, max_null_ratio=0.1, max_age_hours=24.0)
    assert missing == []
