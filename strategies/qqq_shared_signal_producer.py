from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from strategies.qqq_stock_signal_artifact import (
    StockEntrySignalArtifact,
    publish_stock_entry_signal,
    signal_plane_health,
)


@dataclass(frozen=True)
class QQQSharedEntryCandidate:
    mode: str
    v3m_signed: float
    atr_eff: float
    atr_live: float
    atr_seed: float
    atr_cap: float
    vol_surge_ratio: float
    distance_from_vwap_atr: float
    data_age_qqq_sec: Optional[float]
    data_age_qqew_sec: Optional[float]
    continuity_180s_ok: bool
    qqq_now: float
    vwap_now: float
    qqew_now: Optional[float]
    qqew_vwap_now: Optional[float]
    publish_stage: str = "shared_entry_candidate"

    def to_payload(self) -> Dict[str, Any]:
        return {
            "mode": str(self.mode),
            "publish_stage": str(self.publish_stage),
            "signal_plane_family": "qqq_intraday_entry",
            "v3m_signed": float(self.v3m_signed),
            "atr_eff": float(self.atr_eff),
            "atr_live": float(self.atr_live),
            "atr_seed": float(self.atr_seed),
            "atr_cap": float(self.atr_cap),
            "vol_surge_ratio": float(self.vol_surge_ratio),
            "distance_from_vwap_atr": float(self.distance_from_vwap_atr),
            "data_age_qqq_sec": self.data_age_qqq_sec,
            "data_age_qqew_sec": self.data_age_qqew_sec,
            "continuity_180s_ok": bool(self.continuity_180s_ok),
            "qqq_now": float(self.qqq_now),
            "vwap_now": float(self.vwap_now),
            "qqew_now": float(self.qqew_now) if self.qqew_now is not None else None,
            "qqew_vwap_now": float(self.qqew_vwap_now) if self.qqew_vwap_now is not None else None,
        }


def publish_qqq_shared_entry_signal(
    *,
    strategy_id: str,
    trigger_ts: datetime,
    direction: str,
    instrument: str,
    trigger_id: str,
    decision_id: str,
    trace_id: str,
    candidate: QQQSharedEntryCandidate,
) -> Tuple[StockEntrySignalArtifact, Dict[str, Any]]:
    artifact = publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id=str(strategy_id or ""),
        trigger_ts=trigger_ts,
        direction=str(direction or ""),
        instrument=str(instrument or ""),
        entry_allowed=True,
        signal_family="qqq_intraday_entry",
        trigger_id=str(trigger_id or ""),
        decision_id=str(decision_id or ""),
        trace_id=str(trace_id or ""),
        payload=candidate.to_payload(),
    )
    return artifact, signal_plane_health()

