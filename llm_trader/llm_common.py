# llm_common.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import abc
import json

@dataclass
class LLMResponse:
    decision: str                 # BUY / SELL / HOLD
    symbol: str
    quantity: Optional[int] = None
    confidence: float = 0.0
    reasoning: Optional[str] = None
    horizon: Optional[str] = None            # e.g. "scalp_5-30s" / "intraday"
    expected_edge_bps: Optional[float] = None
    raw_response: Optional[str] = None
    provider_name: Optional[str] = None

class BaseLLMProvider(abc.ABC):
    """Provider base class with a unified prompt contract."""
    def __init__(self, api_key: str, model: str, name: str = "provider", **kwargs):
        self.api_key = api_key
        self.model = model
        self.name = name
        # Optional per-model tuning knobs:
        self.timeout_s = kwargs.get("timeout_s", 30)      # slow models can increase this
        self.max_tokens = kwargs.get("max_tokens", 1500)  # clamp outputs

    @abc.abstractmethod
    async def analyze_market(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        *,
        time_context: Dict[str, Any],
        policy_state: Dict[str, Any],
        recent_memory: List[Dict[str, Any]],
    ) -> LLMResponse:
        ...

    # ---- Shared prompt builders (text-only; providers can override if needed) ----
    def build_system_prompt(self) -> str:
        return (
            "You are an expert intraday trader with solid microstructure knowledge. "
            "Make decisions strictly from the provided data. "
            "Always output VALID JSON only."
        )

    def build_user_prompt(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        *,
        time_context: Dict[str, Any],
        policy_state: Dict[str, Any],
        recent_memory: List[Dict[str, Any]],
    ) -> str:
        # choose compact slices to control tokens
        features_5s = market_data.get("features_5s", [])
        last_12_5s = features_5s[:12] if isinstance(features_5s, list) else []
        sfs_features = market_data.get("sfs_features", {})
        one_m = market_data.get("features_1m", [])

        prompt = f"""
# TASK
Decide BUY / SELL / HOLD for {symbol}, considering horizon can be scalp or intraday. 
You may dynamically adapt style based on context and memory.

# POLICY_STATE
{json.dumps(policy_state, ensure_ascii=False)}

# TIME_CONTEXT
{json.dumps(time_context, ensure_ascii=False)}

# RECENT_MEMORY (episodes: latest first)
{json.dumps(recent_memory[:12], ensure_ascii=False)}

# SNAPSHOT (truncated)
current_price: {market_data.get('current_price')}
5s_features_last_12: {json.dumps(last_12_5s, ensure_ascii=False)}
sfs_features(latest): {json.dumps(sfs_features, ensure_ascii=False)}
features_1m_last_5: {json.dumps(one_m[:5] if isinstance(one_m, list) else [], ensure_ascii=False)}

# PORTFOLIO (abbrev)
{json.dumps(portfolio_state, ensure_ascii=False)}

Return ONLY JSON with this schema:
{{
  "decision": "BUY | SELL | HOLD",
  "quantity": <int|null>,
  "confidence": <float 0-1>,
  "reasoning": "<brief>",
  "horizon": "scalp_5-30s | intraday | swing_intraday",
  "expected_edge_bps": <float|null>
}}
"""
        return prompt
