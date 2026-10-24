# gemini_provider.py
"""Google Gemini LLM Provider (unified prompt)"""
from __future__ import annotations
import json, asyncio
from typing import Dict, Any, List

try:
    from . import BaseLLMProvider, LLMResponse
except Exception:
    from llm_common import BaseLLMProvider, LLMResponse

import google.generativeai as genai

class GeminiProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash-exp", **kwargs):
        super().__init__(api_key, model, name="gemini", **kwargs)
        genai.configure(api_key=api_key)
        self.client = genai.GenerativeModel(self.model)

    async def health_check(self) -> bool:
        """Verify the API key is valid by listing models."""
        try:
            models = [m.name for m in genai.list_models()]
            return len(models) > 0
        except Exception:
            return False
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
        try:
            prompt = self.build_user_prompt(
                symbol, market_data, portfolio_state,
                time_context=time_context, policy_state=policy_state, recent_memory=recent_memory
            )
            sys_msg = self.build_system_prompt()
            full = sys_msg + "\n\n" + prompt

            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, self.client.generate_content, full)
            raw = (resp.text or "").strip()

            # strip fences if any
            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

            data = json.loads(raw)
            return LLMResponse(
                decision=(data.get("decision") or "HOLD").upper(),
                symbol=symbol,
                quantity=data.get("quantity"),
                confidence=float(data.get("confidence", 0.0)),
                reasoning=data.get("reasoning"),
                horizon=data.get("horizon"),
                expected_edge_bps=data.get("expected_edge_bps"),
                raw_response=raw,
                provider_name=self.name
            )
        except Exception as e:
            return LLMResponse(
                decision="HOLD", symbol=symbol, confidence=0.0,
                reasoning=f"Gemini error: {e}", raw_response=str(e),
                provider_name=self.name
            )

    async def analyze_market_with_prompt(
        self,
        symbol: str,
        custom_prompt: str,
        portfolio_state: Dict[str, Any] | None = None,
    ) -> LLMResponse:
        """V2 prompt path: send pre-built prompt directly."""
        try:
            # Gemini SDK is sync; run in executor
            loop = asyncio.get_event_loop()
            sys_msg = self.build_system_prompt()
            full_prompt = sys_msg + "\n\n" + custom_prompt
            resp = await loop.run_in_executor(None, self.client.generate_content, full_prompt)
            raw = (resp.text or "").strip()

            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

            data = json.loads(raw)
            return LLMResponse(
                decision=(data.get("decision") or "HOLD").upper(),
                symbol=symbol,
                quantity=data.get("quantity"),
                confidence=float(data.get("confidence", 0.0)),
                reasoning=data.get("reasoning"),
                raw_response=raw,
                provider_name=self.name,
            )
        except Exception as e:
            return LLMResponse(
                decision="HOLD",
                symbol=symbol,
                confidence=0.0,
                reasoning=f"Gemini prompt error: {e}",
                raw_response=str(e),
                provider_name=self.name,
            )
