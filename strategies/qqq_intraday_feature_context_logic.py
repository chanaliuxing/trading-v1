from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict


def apply_feature_context(
    *,
    state: Any,
    decision_signal_price: Any,
    feature_price: float | None,
    features: Dict[str, Any],
    optional_float: Callable[[Any], float | None],
    now_et: datetime,
) -> None:
    entry_ref_px = optional_float(decision_signal_price)
    if entry_ref_px is None:
        entry_ref_px = feature_price
    state.extras["entry_ref_px"] = entry_ref_px
    if state.entry_px is None and entry_ref_px is not None:
        state.entry_px = entry_ref_px

    atr_used = optional_float(features.get("entry_atr_used"))
    if atr_used is None:
        atr_used = optional_float(features.get("atr"))
    if atr_used is not None:
        state.entry_atr_used = atr_used

    trade_target_px = optional_float(features.get("trade_target_px"))
    if trade_target_px is not None:
        state.extras["trade_target_px"] = trade_target_px

    stop_min_abs = optional_float(features.get("stop_min_abs"))
    if stop_min_abs is not None:
        state.extras["stop_min_abs"] = stop_min_abs

    stop_atr_mult = optional_float(features.get("stop_atr_mult"))
    if stop_atr_mult is not None:
        state.extras["stop_atr_mult"] = stop_atr_mult

    stop_dist_pre = optional_float(features.get("stop_dist_pre"))
    if stop_dist_pre is not None:
        state.extras["stop_dist_pre"] = stop_dist_pre
    if state.extras.get("stop_dist_pre") is None and state.entry_px is not None and state.stop_px is not None:
        state.extras["stop_dist_pre"] = abs(float(state.entry_px) - float(state.stop_px))
    if state.extras.get("stop_dist_cur") is None and state.extras.get("stop_dist_pre") is not None:
        state.extras["stop_dist_cur"] = float(state.extras.get("stop_dist_pre"))

    if state.extras.get("entry_time_et") is None:
        state.extras["entry_time_et"] = now_et
