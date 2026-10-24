from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DataProfile:
    profile_id: str
    max_staleness_sec: float
    min_history_bars: int
    session_mode: str


def get_data_profile(profile_id: str) -> DataProfile:
    key = str(profile_id or "").strip() or "default"
    profiles = {
        "default": DataProfile(profile_id="default", max_staleness_sec=8.0, min_history_bars=1, session_mode="mixed"),
        "equity_rth_standard": DataProfile(profile_id="equity_rth_standard", max_staleness_sec=3.0, min_history_bars=15, session_mode="rth"),
        "equity_offhours_standard": DataProfile(profile_id="equity_offhours_standard", max_staleness_sec=8.0, min_history_bars=15, session_mode="offhours"),
        "equity_offhours_tick_only": DataProfile(profile_id="equity_offhours_tick_only", max_staleness_sec=8.0, min_history_bars=0, session_mode="offhours"),
        "qqq_rth_standard": DataProfile(profile_id="qqq_rth_standard", max_staleness_sec=3.0, min_history_bars=15, session_mode="rth"),
        "qqq_offhours_standard": DataProfile(profile_id="qqq_offhours_standard", max_staleness_sec=8.0, min_history_bars=15, session_mode="offhours"),
    }
    if key not in profiles:
        raise KeyError(f"unknown data profile: {key}")
    return profiles[key]
