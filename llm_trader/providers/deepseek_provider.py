# deepseek_provider.py
"""DeepSeek official provider (unified prompt)"""
from __future__ import annotations
import os, json, asyncio
from typing import Dict, Any, List

try:
    from . import BaseLLMProvider, LLMResponse
except Exception:
    from llm_common import BaseLLMProvider, LLMResponse

import httpx

class DeepSeekProvider(BaseLLMProvider):
    """Uses DeepSeek official API (chat completions-like)."""
    def __init__(self, api_key: str, model: str = "deepseek-chat", **kwargs):
        super().__init__(api_key, model, name="deepseek", **kwargs)
        self.base_url = kwargs.get("base_url", "https://api.deepseek.com")
        self.timeout_s = kwargs.get("timeout_s", 60)
        self._client = httpx.AsyncClient(timeout=self.timeout_s)

    async def health_check(self) -> bool:
        """Minimal health check: issue a HEAD to base URL."""
        try:
            r = await self._client.head(self.base_url, timeout=10)
            return r.status_code < 500
        except Exception:
            return False

    async def _call(self, messages: list[dict]) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        r = await self._client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

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
            sys_msg = {"role": "system", "content": self.build_system_prompt()}
            user_msg = {"role": "user", "content": self.build_user_prompt(
                symbol, market_data, portfolio_state,
                time_context=time_context, policy_state=policy_state, recent_memory=recent_memory
            )}
            raw = await self._call([sys_msg, user_msg])

            # try json
            raw_strip = raw.strip()
            if raw_strip.startswith("```json"): raw_strip = raw_strip[7:]
            if raw_strip.startswith("```"): raw_strip = raw_strip[3:]
            if raw_strip.endswith("```"): raw_strip = raw_strip[:-3]
            raw_strip = raw_strip.strip()
            data = json.loads(raw_strip)

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
                reasoning=f"DeepSeek error: {e}", raw_response=str(e),
                provider_name=self.name
            )
