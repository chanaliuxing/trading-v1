"""Local Ollama Provider (Chat API)"""

import json
import asyncio
from typing import Dict, Any, Optional
import httpx

from . import BaseLLMProvider, LLMResponse
from ..utils.llm_logger import log_llm_call


class OllamaProvider(BaseLLMProvider):
    """Ollama local provider using /api/chat"""

    def __init__(self, api_key: str = "", model: str = "qwen2.5vl:latest", **kwargs):
        super().__init__(api_key, model, **kwargs)
        self.base_url = kwargs.get("base_url", "http://localhost:11434")
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 2000)
        self._timeout = kwargs.get("timeout", 300.0)

    def _build_system_prompt(self) -> str:
        return (
            "You are an intraday trading assistant. "
            "Return JSON with keys: decision (BUY/SELL/HOLD), quantity, confidence, reasoning."
        )

    def _extract_json(self, raw_text: str) -> str:
        """Best-effort JSON extraction from model text."""
        if not raw_text:
            return ""
        txt = raw_text.strip()
        # Handle <think> tags if present
        if "<think>" in txt and "</think>" in txt:
            txt = txt.split("</think>")[-1].strip()
            
        if "```json" in txt:
            try:
                return txt.split("```json", 1)[1].split("```", 1)[0].strip()
            except Exception:
                pass
        if "```" in txt:
            try:
                return txt.split("```", 1)[1].split("```", 1)[0].strip()
            except Exception:
                pass
        if not txt.startswith("{"):
            idx = txt.find("{")
            if idx != -1:
                txt = txt[idx:]
        return txt

    @log_llm_call("ollama", "chat")
    async def analyze_market_with_prompt(
        self,
        symbol: str,
        custom_prompt: str,
        portfolio_state: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """Use custom prompt (already assembled by Brain)"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": custom_prompt}],
            "stream": False,
            "options": {
                "num_predict": self.max_tokens,
                "temperature": self.temperature,
            }
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                # Chat API response structure: data['message']['content']
                raw_text = data.get("message", {}).get("content", "") if isinstance(data, dict) else ""
                clean_text = self._extract_json(raw_text)
                try:
                    parsed = json.loads(clean_text) if clean_text else {}
                except Exception:
                    parsed = {}
                return LLMResponse(
                    decision=parsed.get("decision", "HOLD"),
                    symbol=symbol,
                    quantity=parsed.get("quantity"),
                    confidence=parsed.get("confidence", 0.0),
                    reasoning=parsed.get("reasoning", clean_text[:200] if clean_text else raw_text[:200]),
                    raw_response=clean_text or raw_text,
                )
        except Exception as e:
            return LLMResponse(
                decision="HOLD",
                symbol=symbol,
                confidence=0.0,
                reasoning=f"Error: {e}",
                raw_response=str(e),
            )

    async def analyze_market(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
    ) -> LLMResponse:
        # Fall back to a simple prompt build if needed
        prompt = f"{self._build_system_prompt()}\nSymbol: {symbol}\nData: {json.dumps(market_data)[:800]}"
        return await self.analyze_market_with_prompt(symbol, prompt, portfolio_state)

    async def generate_response(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {
                "num_predict": self.max_tokens,
                "temperature": self.temperature,
            }
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "") if isinstance(data, dict) else ""

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                return True
        except Exception:
            return False