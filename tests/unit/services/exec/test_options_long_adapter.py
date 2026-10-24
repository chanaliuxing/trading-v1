import pytest

from services.exec.options_long_adapter import _place_combo_order_sync


def test_legacy_option_combo_direct_ib_execution_is_hard_disabled(monkeypatch):
    monkeypatch.setenv("OPTION_ML_EXECUTION_ENABLED", "1")

    with pytest.raises(RuntimeError, match="legacy_option_combo_direct_ib_disabled"):
        _place_combo_order_sync(
            underlying="QQQ",
            legs=[],
            qty=1,
            limit_price=1.0,
            order_ref="test",
            side="BUY",
        )


def test_legacy_option_combo_noops_when_execution_disabled(monkeypatch):
    monkeypatch.delenv("OPTION_ML_EXECUTION_ENABLED", raising=False)

    assert _place_combo_order_sync(
        underlying="QQQ",
        legs=[],
        qty=1,
        limit_price=1.0,
        order_ref="test",
        side="BUY",
    ) is False
