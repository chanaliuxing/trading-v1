"""Minimal LLM client with offline fallback."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, List, Callable

import requests
from requests import Response, Session

from ops.secrets import get_secret, SecretError


class LLMClient:
    """Thin wrapper around an OpenAI-compatible API with safe fallback."""

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        default_host = "https://api.openai.com"
        self.base_url = base_url or os.getenv("LLM_BASE_URL", default_host)
        self.ollama_base = os.getenv("OLLAMA_BASE", "http://localhost:11434")
        try:
            self.api_key = get_secret("LLM_API_KEY", required=False)
        except SecretError:
            self.api_key = ""
        self.timeout = timeout
        self._session: Optional[Session] = None

    # ------------------------------------------------------------------
    def generate(self, system_prompt: str, user_prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
        target_model = model or self.model
        # Prefer Ollama when pointing at localhost or model is an Ollama tag
        if self._should_use_ollama(target_model):
            try:
                content = self._call_ollama(system_prompt, user_prompt, target_model)
                return self._safe_json(content, user_prompt)
            except Exception:
                return self._offline_fallback(user_prompt)

        if not self.api_key:
            return self._offline_fallback(user_prompt)
        try:
            if self._should_use_responses_api():
                resp = self._call_responses_api(system_prompt, user_prompt)
                content = self._extract_responses_text(resp)
            else:
                resp = self._call_chat_completions(system_prompt, user_prompt, target_model)
                content = resp["choices"][0]["message"]["content"]
            return self._safe_json(content, user_prompt)
        except Exception:
            return self._offline_fallback(user_prompt)

    def generate_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_callbacks: Dict[str, Callable],
    ) -> Dict[str, Any]:
        """Generates a response from the LLM, handling tool calls in a loop."""
        if not self.api_key:
            return self._offline_fallback(user_prompt)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        while True:
            response = self._call_chat_completions_with_tools(messages, tools)
            message = response["choices"][0]["message"]

            if message.get("tool_calls"):
                tool_calls = message["tool_calls"]
                messages.append(message)  # Add assistant's message with tool calls

                for tool_call in tool_calls:
                    function_name = tool_call["function"]["name"]
                    function_args = json.loads(tool_call["function"]["arguments"])

                    if function_name in tool_callbacks:
                        function_to_call = tool_callbacks[function_name]
                        try:
                            function_response = function_to_call(**function_args)
                        except Exception as e:
                            function_response = {"error": str(e)}
                    else:
                        function_response = {
                            "error": f"Tool '{function_name}' not found."
                        }

                    messages.append(
                        {
                            "tool_call_id": tool_call["id"],
                            "role": "tool",
                            "name": function_name,
                            "content": json.dumps(function_response),
                        }
                    )
            else:
                # No tool call, this is the final answer
                content = message["content"]
                text = content.strip().strip("`")
                if text.startswith("json"):
                    text = text[4:].strip()
                return json.loads(text)

    # ------------------------------------------------------------------
    def _should_use_responses_api(self) -> bool:
        model = (self.model or "").lower()
        if model.startswith("gpt-5"):
            return True
        preferred = os.getenv("LLM_API_STYLE", "").lower()
        return preferred in {"responses", "response"} or model in {"o4-mini"}

    def _should_use_ollama(self, model: str) -> bool:
        if self.base_url.startswith("http://localhost:11434") or self.base_url.startswith("http://127.0.0.1:11434"):
            return True
        local_models = {m.strip().lower() for m in os.getenv("LLM_LOCAL_MODELS", "").split(",") if m.strip()}
        return model.lower() in local_models

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _session_post(self, url: str, payload: Dict[str, Any]) -> Response:
        if self._session is None:
            self._session = requests.Session()
        resp = self._session.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def _call_chat_completions(self, system_prompt: str, user_prompt: str, model: str) -> Dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        url = f"{self.base_url.rstrip('/')}/v1/chat/completions"
        resp = self._session_post(url, payload)
        return resp.json()

    def _call_chat_completions_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calls the chat completions API with tools."""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        url = f"{self.base_url.rstrip('/')}/v1/chat/completions"
        resp = self._session_post(url, payload)
        return resp.json()

    def _call_ollama(self, system_prompt: str, user_prompt: str, model: str) -> str:
        payload = {
            "model": model,
            "prompt": f"{system_prompt}\n\n{user_prompt}",
            "stream": False,
        }
        url = f"{self.ollama_base.rstrip('/')}/api/generate"
        resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response") or data.get("output") or ""

    def _call_responses_api(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
            ],
            "temperature": 0.2,
            "reasoning": {"effort": os.getenv("LLM_REASONING_EFFORT", "low")},
            "text": {"verbosity": os.getenv("LLM_VERBOSITY", "low")},
        }
        url = f"{self.base_url.rstrip('/')}/v1/responses"
        resp = self._session_post(url, payload)
        return resp.json()

    @staticmethod
    def _extract_responses_text(resp_json: Dict[str, Any]) -> str:
        output = resp_json.get("output") or resp_json.get("response") or resp_json
        if isinstance(output, dict):
            results = output.get("output") or output.get("content") or []
        else:
            results = output
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    if item.get("type") == "output_text":
                        return item.get("text", "")
                    if "content" in item and isinstance(item["content"], list):
                        for part in item["content"]:
                            if part.get("type") == "output_text":
                                return part.get("text", "")
                            if part.get("type") == "text":
                                return part.get("text", "")
        message = resp_json.get("message") or {}
        if isinstance(message, dict):
            parts = message.get("content") or []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
        raise ValueError("Unable to extract text from responses payload")

    def _safe_json(self, content: str, fallback_prompt: str) -> Dict[str, Any]:
        text = (content or "").strip()
        # Strip common wrappers/backticks/prefixes
        for prefix in ["```json", "```", "json"]:
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
        if text.endswith("```"):
            text = text[:-3].strip()

        # Heuristic: extract first JSON object/array if present
        def _extract_json_fragment(t: str) -> str:
            stack = []
            start = None
            for i, ch in enumerate(t):
                if ch in "{[":
                    if start is None:
                        start = i
                    stack.append(ch)
                elif ch in "}]":
                    if stack:
                        stack.pop()
                        if not stack and start is not None:
                            return t[start : i + 1]
            return t

        candidate = _extract_json_fragment(text)
        try:
            return json.loads(candidate)
        except Exception:
            return {"text": text, "from": "llm", "prompt": fallback_prompt}

    # ------------------------------------------------------------------
    @staticmethod
    def _offline_fallback(prompt: str) -> Dict[str, Any]:  # pragma: no cover - deterministic fallback
        return {
            "feature_recipe": {
                "use_table": "features_1m",
                "base_features": ["close", "vwap", "ema_9", "ema_21", "rsi_14", "atr_14", "volume"],
                "derived": [
                    {"alias": "ret_1", "expr": "log(close/lag(close,1))"},
                    {"alias": "mom_9", "expr": "ema_9 - ema_21"},
                    {"alias": "atr_norm", "expr": "atr_14 / nullif(close,0)"},
                ],
                "filters": ["close > 0", "volume > 0"],
            },
            "training_plan": {
                "symbol": "TSLA",
                "horizon": "5m",
                "task": "reg",
                "target": {
                    "alias": "y",
                    "expr": "log(lead(close,5)/close)",
                },
                "model_candidates": [
                    {
                        "model_type": "xgb",
                        "model_params": {
                            "n_estimators": 400,
                            "max_depth": 6,
                            "learning_rate": 0.05,
                            "subsample": 0.9,
                            "colsample_bytree": 0.8,
                            "early_stopping_rounds": 50,
                        },
                    },
                    {
                        "model_type": "lstm",
                        "model_params": {
                            "lookback": 32,
                            "hidden": 64,
                            "num_layers": 1,
                            "dropout": 0.0,
                            "lr": 0.001,
                            "batch_size": 256,
                            "epochs": 80,
                            "patience": 8,
                        },
                    },
                ],
                "decision": {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
                "optuna_hints": {"n_trials": 40},
            },
        }
