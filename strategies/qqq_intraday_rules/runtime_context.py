from __future__ import annotations

from typing import Any, Dict

from strategies.options_kernel.types import PluginRuntimeState

from .contracts import EntryContext
from .session import resolve_window
from .truth_fabric import _MinuteRow, _compute_atr_from_rows, resolve_qqq_truth_fabric


def build_entry_context(runtime: PluginRuntimeState, extras: Dict[str, Any]) -> EntryContext:
    truth = resolve_qqq_truth_fabric(runtime, extras)
    entry_model = str(truth.extras.get("entry_model", "baseline")).strip().lower()
    gate_start, gate_end = resolve_window(entry_model, truth.extras)
    return EntryContext(
        now=truth.now,
        symbol=truth.symbol,
        entry_model=entry_model,
        gate_start=gate_start,
        gate_end=gate_end,
        qqq_now=float(truth.spot),
        qqq_ago=truth.qqq_ago,
        qqew_now=truth.qqew_now,
        qqew_ago=truth.qqew_ago,
        vwap_now=float(truth.vwap),
        qqew_vwap_now=truth.qqew_vwap_now,
        v3m_signed=float(truth.v3m_signed),
        atr_live=float(truth.atr_live),
        atr_seed=float(truth.atr_seed),
        atr_eff=float(truth.atr_eff),
        atr_cap=float(truth.atr_cap),
        vol_t=float(truth.vol_t),
        vol_ma10=truth.vol_ma10,
        vol_surge_ratio=float(truth.vol_surge_ratio),
        distance_from_vwap_atr=float(truth.distance_from_vwap_atr),
        data_age_qqq_sec=truth.data_age_qqq_sec,
        data_age_qqew_sec=truth.data_age_qqew_sec,
        continuity_180s_ok=bool(truth.continuity_180s_ok),
        qqew_decoupled=bool(truth.qqew_decoupled),
        close_by_minute=dict(truth.close_by_minute),
        vwap_by_minute=dict(truth.vwap_by_minute),
        vol_surge_by_minute=dict(truth.vol_surge_by_minute),
        live_samples=list(truth.live_samples),
        session_open_price=truth.session_open_price,
        day_position_rank=truth.day_position_rank,
        extras=dict(truth.extras),
    )
