from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class OptionBarContext:
    now: datetime
    bar: Dict[str, Any]
    wall_now: Optional[datetime] = None


@dataclass
class OptionQuoteRequest:
    now: datetime
    side: str
    spot: float
    spread_max_pct: float
    now_for_quote_age: Optional[datetime] = None


@dataclass
class OptionQuoteSelection:
    quote: Optional[Dict[str, Any]]
    reject_reason: Optional[str] = None
