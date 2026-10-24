# deepseek_nvidia_provider.py
"""DeepSeek via NVIDIA NIM / cloud endpoints (unified prompt)"""
from __future__ import annotations
import os, json, asyncio
from typing import Dict, Any, List

try:
    from . import BaseLLMProvider, LLMResponse
except Exception:
    from llm_common import BaseLLMProvider, LLMResponse

import httpx

class DeepSeekNvidiaProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str = "deepseek-r1-nv", **kwargs):
        super().__init__(api_key, model, name="deepseek_nvidia", **kwargs)
        self.base_url = kwargs.get("base_url", os.environ.get("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1"))
        self.timeout_s = kwargs.get("timeout_s", 60) # Default timeout
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        self._header_name = kwargs.get("header_name", "Authorization")
        self._header_prefix = kwargs.get("header_prefix", "Bearer")

    async def health_check(self) -> bool:
        """Minimal health check: issue a HEAD to base URL."""
        try:
            r = await self._client.head(self.base_url, timeout=10)
            return r.status_code < 500
        except Exception:
            return False

    async def _call(self, messages: list[dict]) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {self._header_name: f"{self._header_prefix} {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        r = await self._client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

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

            raw2 = raw.strip()
            if raw2.startswith("```json"): raw2 = raw2[7:]
            if raw2.startswith("```"): raw2 = raw2[3:]
            if raw2.endswith("```"): raw2 = raw2[:-3]
            raw2 = raw2.strip()

            data = json.loads(raw2)
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
                reasoning=f"DeepSeek NVIDIA error: {e}", raw_response=str(e),
                provider_name=self.name
            )

    async def analyze_market_with_prompt(
        self,
        symbol: str,
        custom_prompt: str,
        portfolio_state: Dict[str, Any] | None = None,
    ) -> LLMResponse:
        """V2 prompt path: send custom prompt directly."""
        try:
            sys_msg = {"role": "system", "content": self.build_system_prompt()}
            user_msg = {"role": "user", "content": custom_prompt}
            raw = await self._call([sys_msg, user_msg])

            cleaned = raw.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            data = json.loads(cleaned)
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
                reasoning=f"DeepSeek NVIDIA prompt error: {e}",
                raw_response=str(e),
                provider_name=self.name,
            )
