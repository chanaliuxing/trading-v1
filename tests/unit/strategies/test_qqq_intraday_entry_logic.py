from __future__ import annotations

from strategies.qqq_intraday_entry_logic import build_probe_add_plan, check_day_gross_notional_cap


def test_build_probe_add_plan_uses_probe_ratio() -> None:
    plan = build_probe_add_plan(qty_total=229, probe_ratio=0.70)
    assert plan.qty_total == 229
    assert plan.probe_qty == 160
    assert plan.add_qty == 69


def test_build_probe_add_plan_falls_back_to_all_probe_when_small() -> None:
    plan = build_probe_add_plan(qty_total=1, probe_ratio=0.0)
    assert plan.probe_qty == 1
    assert plan.add_qty == 0


def test_check_day_gross_notional_cap_blocks_when_over_cap() -> None:
    result = check_day_gross_notional_cap(
        cap=1000.0,
        existing_long_qty=5.0,
        existing_short_qty=0.0,
        long_price=100.0,
        short_price=0.0,
        probe_qty=6,
        entry_price=100.0,
    )
    assert result.allowed is False
    assert result.existing_gross_notional == 500.0
    assert result.planned_notional == 600.0


def test_check_day_gross_notional_cap_allows_when_within_cap() -> None:
    result = check_day_gross_notional_cap(
        cap=1200.0,
        existing_long_qty=5.0,
        existing_short_qty=0.0,
        long_price=100.0,
        short_price=0.0,
        probe_qty=6,
        entry_price=100.0,
    )
    assert result.allowed is True
