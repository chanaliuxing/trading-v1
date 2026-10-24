# openai_provider.py
"""OpenAI LLM Provider (JSON-mode, unified prompt)"""
from __future__ import annotations
import asyncio, json
from typing import Dict, Any, List

try:
    # preferred relative import (if package layout exists)
    from . import BaseLLMProvider, LLMResponse
except Exception:
    from llm_common import BaseLLMProvider, LLMResponse

from openai import AsyncOpenAI

class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", **kwargs):
        super().__init__(api_key, model, name="openai", **kwargs)
        self.client = AsyncOpenAI(api_key=api_key)

    async def health_check(self) -> bool:
        """Verify the API key is valid by listing models."""
        try:
            # Note: client.models.list() is a SyncPage, so iterate to check
            models = await self.client.models.list()
            for _ in models:
                return True
            return False
        except Exception:
            return False

    def _system(self) -> str:
        return self.build_system_prompt()

    def _user(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        *,
        time_context: Dict[str, Any],
        policy_state: Dict[str, Any],
        recent_memory: List[Dict[str, Any]],
    ) -> str:
        return self.build_user_prompt(
            symbol, market_data, portfolio_state,
            time_context=time_context, policy_state=policy_state, recent_memory=recent_memory
        )

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
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system()},
                    {"role": "user", "content": self._user(
                        symbol, market_data, portfolio_state,
                        time_context=time_context, policy_state=policy_state, recent_memory=recent_memory
                    )},
                ],
                response_format={"type": "json_object"},
                timeout=self.timeout_s,
                max_tokens=self.max_tokens,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content.strip()
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
                reasoning=f"OpenAI error: {e}", raw_response=str(e),
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
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system()},
                    {"role": "user", "content": custom_prompt},
                ],
                response_format={"type": "json_object"},
                timeout=self.timeout_s,
                max_tokens=self.max_tokens,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content.strip()
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
                reasoning=f"OpenAI prompt error: {e}",
                raw_response=str(e),
                provider_name=self.name,
            )
