from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PortfolioExposureDecision:
    allowed: bool
    reason: str
    gross_exposure: float
    net_exposure: float
    payload: dict[str, Any]


class PortfolioExposurePolicy:
    """
    Backward-compatible exposure policy.
    """

    def __init__(
        self,
        *,
        gross_limit: float = 0.0,
        net_limit: float = 0.0,
        symbol_limit: float = 0.0,
    ) -> None:
        self._gross_limit = max(0.0, float(gross_limit or 0.0))
        self._net_limit = max(0.0, float(net_limit or 0.0))
        self._symbol_limit = max(0.0, float(symbol_limit or 0.0))

    def evaluate(
        self,
        *,
        current_positions: tuple[Any, ...],
        candidate_symbol: str,
        candidate_qty: float,
        candidate_price: float | None,
    ) -> PortfolioExposureDecision:
        gross = 0.0
        net = 0.0
        symbol_u = str(candidate_symbol or "").strip().upper()
        symbol_exposure = 0.0
        for pos in current_positions:
            qty = float(getattr(pos, "qty", 0.0) or 0.0)
            px = float(getattr(pos, "price", 0.0) or 0.0)
            notional = abs(qty) * px
            gross += notional
            net += qty * px
            if str(getattr(pos, "symbol", "") or "").strip().upper() == symbol_u:
                symbol_exposure += notional
        candidate_notional = abs(float(candidate_qty or 0.0)) * max(0.0, float(candidate_price or 0.0))
        gross_after = gross + candidate_notional
        net_after = net + candidate_notional
        symbol_after = symbol_exposure + candidate_notional
        if self._gross_limit > 0.0 and gross_after > self._gross_limit:
            return PortfolioExposureDecision(False, "gross_exposure_limit", gross_after, net_after, {"symbol_exposure": symbol_after})
        if self._net_limit > 0.0 and abs(net_after) > self._net_limit:
            return PortfolioExposureDecision(False, "net_exposure_limit", gross_after, net_after, {"symbol_exposure": symbol_after})
        if self._symbol_limit > 0.0 and symbol_after > self._symbol_limit:
            return PortfolioExposureDecision(False, "symbol_exposure_limit", gross_after, net_after, {"symbol_exposure": symbol_after})
        return PortfolioExposureDecision(True, "ready", gross_after, net_after, {"symbol_exposure": symbol_after})
