from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from strategies.qqq_intraday_feature_context_logic import apply_feature_context


def _opt_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def test_apply_feature_context_sets_primary_fields() -> None:
    state = SimpleNamespace(entry_px=None, stop_px=48.5, entry_atr_used=None, extras={})
    now = datetime.fromisoformat("2026-03-25T09:45:00-04:00")
    apply_feature_context(
        state=state,
        decision_signal_price=50.0,
        feature_price=None,
        features={"trade_target_px": 52.0, "stop_min_abs": 0.5, "stop_atr_mult": 2.5},
        optional_float=_opt_float,
        now_et=now,
    )
    assert state.entry_px == 50.0
    assert state.extras["entry_ref_px"] == 50.0
    assert state.extras["trade_target_px"] == 52.0
    assert state.extras["stop_min_abs"] == 0.5
    assert state.extras["stop_atr_mult"] == 2.5
    assert state.extras["entry_time_et"] == now


def test_apply_feature_context_uses_fallback_and_derives_stop_dist() -> None:
    state = SimpleNamespace(entry_px=None, stop_px=48.5, entry_atr_used=None, extras={})
    now = datetime.fromisoformat("2026-03-25T09:46:00-04:00")
    apply_feature_context(
        state=state,
        decision_signal_price=None,
        feature_price=50.0,
        features={"atr": 0.7},
        optional_float=_opt_float,
        now_et=now,
    )
    assert state.entry_px == 50.0
    assert state.entry_atr_used == 0.7
    assert state.extras["stop_dist_pre"] == 1.5
    assert state.extras["stop_dist_cur"] == 1.5


def test_apply_feature_context_does_not_override_existing_entry_time() -> None:
    existing = datetime.fromisoformat("2026-03-25T09:30:00-04:00")
    state = SimpleNamespace(entry_px=50.0, stop_px=49.0, entry_atr_used=None, extras={"entry_time_et": existing})
    now = datetime.fromisoformat("2026-03-25T10:00:00-04:00")
    apply_feature_context(
        state=state,
        decision_signal_price=50.2,
        feature_price=None,
        features={},
        optional_float=_opt_float,
        now_et=now,
    )
    assert state.extras["entry_time_et"] == existing
