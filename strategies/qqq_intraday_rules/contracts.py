from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, Literal, Optional


Direction = Literal["LONG", "SHORT", "NONE"]


@dataclass(frozen=True)
class EntryContext:
    now: datetime
    symbol: str
    entry_model: str
    gate_start: time
    gate_end: time
    qqq_now: float
    qqq_ago: Optional[float]
    qqew_now: Optional[float]
    qqew_ago: Optional[float]
    vwap_now: float
    qqew_vwap_now: Optional[float]
    v3m_signed: float
    atr_live: float
    atr_seed: float
    atr_eff: float
    atr_cap: float
    vol_t: float
    vol_ma10: Optional[float]
    vol_surge_ratio: float
    distance_from_vwap_atr: float
    data_age_qqq_sec: Optional[float]
    data_age_qqew_sec: Optional[float]
    continuity_180s_ok: bool
    qqew_decoupled: bool
    close_by_minute: Dict[datetime, float] = field(default_factory=dict)
    vwap_by_minute: Dict[datetime, float] = field(default_factory=dict)
    vol_surge_by_minute: Dict[datetime, float] = field(default_factory=dict)
    live_samples: list[tuple[datetime, float]] = field(default_factory=list)
    session_open_price: Optional[float] = None
    day_position_rank: Optional[float] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectionDecision:
    direction: Direction
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranslationDecision:
    allowed: bool
    reason: str
    side: Optional[str]
    stock_side: Direction
    payload: Dict[str, Any] = field(default_factory=dict)
