from __future__ import annotations

from typing import Optional

from strategies.options_kernel.types import Selection
from strategies.options_plugins.ma_1dte_reject_shared import select_option_quote_for_underlying

from .contract_policy import BaseContractPolicy, ContractSelectionContext


class StandardOptionContractPolicy(BaseContractPolicy):
    def __init__(self, policy_id: str = "standard_option_contract") -> None:
        self.policy_id = policy_id
        self.last_reject_reason: str = ""
        self.last_reject_diagnostics: dict[str, object] = {}

    @staticmethod
    def _normalise_reject_code(reason: str) -> str:
        value = str(reason or "").strip().upper()
        if not value:
            return "UNKNOWN"
        known = {
            "NO_QUOTES",
            "STALE_QUOTE",
            "NO_VALID_QUOTE",
            "SPREAD_TOO_WIDE",
            "DELTA_OUT_OF_RANGE",
            "OI_LOW",
            "QUOTE_AGE",
            "CHAIN_EMPTY",
            "RISK_VETO",
        }
        if value in known:
            return value
        for prefix in known:
            if value.startswith(prefix):
                return prefix
        if "SPREAD" in value:
            return "SPREAD_TOO_WIDE"
        if "DELTA" in value:
            return "DELTA_OUT_OF_RANGE"
        if "OPEN_INTEREST" in value or "OI" in value:
            return "OI_LOW"
        if "STALE" in value or "AGE" in value:
            return "QUOTE_AGE"
        if "QUOTE" in value:
            return "NO_QUOTES"
        return value

    def select_contract(self, ctx: ContractSelectionContext) -> Optional[Selection]:
        extras = dict(ctx.extras or {})
        runtime = ctx.runtime
        entry_plan = ctx.entry_plan
        quote, reason = select_option_quote_for_underlying(
            underlying=str(extras.get("underlying") or runtime.symbol),
            extras=extras,
            now=runtime.now,
            side=str(entry_plan.side),
            spot=float(runtime.spot or 0.0),
            spread_max_pct=float(extras.get("spread_max_pct", 0.12) or 0.12),
            now_for_quote_age=runtime.now,
        )
        self.last_reject_reason = str(reason or "")
        self.last_reject_diagnostics = {
            "reason": self.last_reject_reason,
            "code": self._normalise_reject_code(self.last_reject_reason),
            "policy_id": self.policy_id,
            "underlying": str(extras.get("underlying") or runtime.symbol),
            "side": str(entry_plan.side),
            "spot": float(runtime.spot or 0.0),
            "spread_max_pct": float(extras.get("spread_max_pct", 0.12) or 0.12),
            "dte_preferred": extras.get("dte_preferred"),
            "dte_fallback_max": extras.get("dte_fallback_max"),
            "min_open_interest": extras.get("min_open_interest"),
        }
        if quote is None:
            return None
        return Selection(
            opt_symbol=str(quote.get("opt_symbol") or ""),
            expiry=str(quote.get("expiry") or ""),
            strike=float(quote.get("strike") or 0.0),
            side=str(entry_plan.side),
            bid=float(quote.get("bid") or 0.0),
            ask=float(quote.get("ask") or 0.0),
            mid=float(quote.get("mid") or 0.0),
            last=float(quote.get("last") or 0.0),
            quote_age_sec=float(quote.get("quote_age_sec") or 0.0),
            payload={"reject_reason": self.last_reject_reason},
        )
