"""
LLM Provider Fallback Mechanism

提供多 Provider 故障切换能力，避免单点故障。

功能:
1. 配置 Provider 优先级列表
2. 自动 Fallback 到下一个 Provider
3. 记录失败和切换日志
4. 支持超时和重试策略

使用方法:
    from brain.provider_fallback import ProviderPool, ProviderConfig

    pool = ProviderPool(configs=[
        ProviderConfig(name="deepseek_nvidia", priority=1, timeout=60),
        ProviderConfig(name="gemini", priority=2, timeout=30),
        ProviderConfig(name="openai", priority=3, timeout=45),
    ])

    result, raw, provider_used = await pool.call_with_fallback(prompt, "propose_plan")
"""

import os
import asyncio
import json
from typing import Dict, Any, Optional, List, Tuple, NamedTuple
from dataclasses import dataclass
from datetime import datetime
from libs.logger import get_logger


class LLMCallResult(NamedTuple):
    """LLM 调用结果"""
    data: Dict[str, Any]  # 解析后的 JSON
    raw: str  # 原始响应
    provider_used: str  # 实际使用的 provider
    success: bool  # 是否成功
    error_type: str  # 错误类型: '' | 'timeout' | 'api_error' | 'json_parse_error' | 'all_providers_failed'
    error_message: str  # 错误详细信息
    fallback_used: bool  # 是否使用了 fallback
    providers_attempted: List[str]  # 尝试过的 provider 列表


# Provider 配置映射 (从 brain.py 复制)
PROVIDER_REGISTRY = {
    "deepseek_nvidia": (
        "llm_trader.providers.deepseek_nvidia_provider",
        "DeepSeekNvidiaProvider",
        "NVIDIA_API_KEY",
    ),
    "openai": (
        "llm_trader.providers.openai_provider",
        "OpenAIProvider",
        "OPENAI_API_KEY",
    ),
    "gemini": (
        "llm_trader.providers.gemini_provider",
        "GeminiProvider",
        "GEMINI_API_KEY",
    ),
    "deepseek": (
        "llm_trader.providers.deepseek_provider",
        "DeepSeekProvider",
        "DEEPSEEK_API_KEY",
    ),
    "ollama": (
        "llm_trader.providers.ollama_provider",
        "OllamaProvider",
        None,  # no API key required for local Ollama
    ),
}


@dataclass
class ProviderConfig:
    """Provider 配置"""
    name: str
    priority: int  # 1 = highest priority
    timeout: float = 60.0  # seconds
    max_retries: int = 1  # 每个 provider 的重试次数
    model: Optional[str] = None  # 如果None，使用默认模型


class ProviderPool:
    """
    LLM Provider 池，支持自动 Fallback

    特性:
    - 按优先级顺序尝试 Provider
    - 支持超时和重试
    - 记录失败和切换日志
    - 统计每个 Provider 的成功率
    """

    def __init__(
        self,
        configs: List[ProviderConfig],
        runs_dir: Optional[str] = None,
        log_failures: bool = True
    ):
        """
        Args:
            configs: Provider 配置列表
            runs_dir: 日志目录（用于记录 fallback 事件）
            log_failures: 是否记录失败到文件
        """
        self.configs = sorted(configs, key=lambda x: x.priority)
        self.runs_dir = runs_dir
        self.log_failures = log_failures
        self.log = get_logger("provider_pool")

        # Provider 实例缓存
        self._provider_cache: Dict[str, Any] = {}

        # 统计信息
        self.stats = {
            provider.name: {
                'calls': 0,
                'successes': 0,
                'failures': 0,
                'timeouts': 0,
                'fallbacks_from': 0  # 从这个 provider 切换到下一个的次数
            }
            for provider in self.configs
        }

    def _build_configs_from_env(self, raw: str) -> List[ProviderConfig]:
        """Parse a provider chain string into ProviderConfigs."""
        default_ollama_timeout = int(os.getenv("OLLAMA_DEFAULT_TIMEOUT", 300))
        default_model = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5vl:latest")
        entries = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            timeout = None
            model = None
            if "@" in part:
                part, t = part.split("@", 1)
                try:
                    timeout = int(t)
                except Exception:
                    timeout = None
            if ":" in part:
                provider, model = part.split(":", 1)
            else:
                provider = part
            if model is None and provider == "ollama":
                model = default_model
            if timeout is None:
                if provider == "ollama":
                    timeout = default_ollama_timeout
                elif provider == "deepseek_nvidia":
                    timeout = 300
                elif provider == "openai":
                    timeout = 45
                else:
                    timeout = 30
            entries.append((provider, model, timeout))

        configs = []
        for idx, (provider, model, timeout) in enumerate(entries, start=1):
            configs.append(
                ProviderConfig(
                    name=provider,
                    model=model,
                    priority=idx,
                    timeout=timeout,
                    max_retries=1
                )
            )
        return configs

    def _get_or_create_provider(self, config: ProviderConfig) -> Any:
        """获取或创建 Provider 实例"""
        cache_key = f"{config.name}:{config.model or 'default'}"

        if cache_key in self._provider_cache:
            return self._provider_cache[cache_key]

        # 创建 Provider
        if config.name not in PROVIDER_REGISTRY:
            raise ValueError(f"Unknown provider: {config.name}")

        mod_name, cls_name, env_key = PROVIDER_REGISTRY[config.name]
        api_key = os.getenv(env_key) if env_key else ""
        if env_key and not api_key:
            raise RuntimeError(f"Missing API key for {config.name}: env {env_key}")

        # 动态导入
        mod = __import__(mod_name, fromlist=[cls_name])
        cls = getattr(mod, cls_name)

        # 获取默认模型
        model = config.model
        if not model:
            # 使用每个 provider 的默认模型
            model = self._get_default_model(config.name)

        kwargs = {}
        if config.name == "ollama":
            base_url = (
                os.getenv("OLLAMA_BASE_URL")
                or os.getenv("OLLAMA_URL")
                or os.getenv("OLLAMA_HOST")
                or "http://localhost:11434"
            )
            kwargs["base_url"] = base_url
        provider = cls(api_key, model, **kwargs)
        self._provider_cache[cache_key] = provider

        self.log.info(
            "provider_created",
            name=config.name,
            model=model,
            timeout=config.timeout
        )

        return provider

    def _get_default_model(self, provider_name: str) -> str:
        """获取 Provider 默认模型"""
        defaults = {
            "deepseek_nvidia": "deepseek-ai/deepseek-r1",
            "openai": "gpt-4o-mini",
            "gemini": "gemini-2.0-flash-exp",
            "deepseek": "deepseek-chat",
            "ollama": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5vl:latest"),
        }
        return defaults.get(provider_name, "default")

    async def _call_provider(
        self,
        config: ProviderConfig,
        prompt: str,
        operation: str,
        attempt: int = 1
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        调用单个 Provider

        Returns:
            (success, parsed_json, raw_response, error_msg)
        """
        provider_name = config.name
        self.stats[provider_name]['calls'] += 1

        try:
            provider = self._get_or_create_provider(config)

            # 调用 Provider (with timeout)
            start_time = datetime.now()

            # Prefer analyze_market_with_prompt if available; tolerate providers with different signatures
            if hasattr(provider, "analyze_market_with_prompt"):
                try:
                    resp = await asyncio.wait_for(
                        provider.analyze_market_with_prompt("SPY", prompt, {}),  # portfolio_state optional
                        timeout=config.timeout
                    )
                except TypeError:
                    # Fallback: assume signature (symbol, prompt)
                    resp = await asyncio.wait_for(
                        provider.analyze_market_with_prompt("SPY", prompt),
                        timeout=config.timeout
                    )
            else:
                resp = await asyncio.wait_for(
                    provider.analyze_market("SPY", {}, {}),
                    timeout=config.timeout
                )

            duration_ms = (datetime.now() - start_time).total_seconds() * 1000
            raw = resp.raw_response or "{}"
            
            # DEBUG: Log raw content
            self.log.info("raw_llm_response_received", length=len(raw), sample=raw[:200])

            try:
                data = json.loads(raw)
            except Exception:
                data = self._extract_json(raw)

            if self._is_transport_or_gateway_error(raw):
                raise ValueError("provider_transport_error_response")

            # For review_ml, synthesize minimal structure if the model didn't follow schema
            if operation == "review_ml":
                if not isinstance(data, dict):
                    data = {}
                if not data.get("summary"):
                    data["summary"] = (raw or "")[:200] or "No structured summary"
                if not data.get("actions"):
                    data["actions"] = []
            if operation == "research_director":
                if not isinstance(data, dict):
                    data = {}
                data.setdefault("experiments", [])
                data.setdefault("infra_experiments", [])
                if not data.get("focus") and not data.get("experiments") and not data.get("policy_patch_candidates"):
                    raise ValueError("research_director_empty_plan")
            # Validate structured output:
            # - review_ml / research_director / research_plan: accept any non-empty dict
            # - other ops: require "decision"
            allow_missing_decision = {
                "review_ml", "research_director", "research_plan", "daily_summary",
                "propose_plan", "propose_plan_retry", "maintenance", "plan_research",
                "critic_validate"
            }
            if not isinstance(data, dict) or not data:
                raise ValueError("Invalid JSON structure from provider")
            
            is_allowed = operation in allow_missing_decision or operation.startswith("decide_")
            if not is_allowed and "decision" not in data:
                raise ValueError(f"Missing decision in provider response (op={operation})")

            # 成功
            self.stats[provider_name]['successes'] += 1

            self.log.info(
                "provider_call_success",
                provider=provider_name,
                attempt=attempt,
                duration_ms=round(duration_ms, 1),
                response_len=len(raw)
            )

            return True, data, raw, None

        except asyncio.TimeoutError:
            self.stats[provider_name]['timeouts'] += 1
            error_msg = f"Timeout after {config.timeout}s"

            self.log.warning(
                "provider_timeout",
                provider=provider_name,
                attempt=attempt,
                timeout=config.timeout
            )

            return False, None, None, error_msg

        except (NotImplementedError, ValueError, json.JSONDecodeError) as e:
            # Treat functional errors (bad JSON, missing method) as failures to trigger fallback
            self.stats[provider_name]['failures'] += 1
            error_msg = f"{type(e).__name__}: {str(e)}"

            self.log.warning(
                "provider_functional_failure",
                provider=provider_name,
                attempt=attempt,
                error=error_msg
            )
            return False, None, None, error_msg

        except Exception as e:
            self.stats[provider_name]['failures'] += 1
            error_msg = str(e)

            self.log.error(
                "provider_call_failed",
                provider=provider_name,
                attempt=attempt,
                error=error_msg,
                exc_info=True
            )

            return False, None, None, error_msg

    @staticmethod
    def _is_transport_or_gateway_error(raw: str) -> bool:
        text = str(raw or "").strip().lower()
        if not text:
            return False
        markers = (
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "http/1.1 50",
            "server error",
            "connection refused",
            "timed out",
            "temporarily unavailable",
        )
        return any(marker in text for marker in markers)

    def _extract_json(self, raw_text: str) -> Dict[str, Any]:
        """从原始响应中提取 JSON (增强版: Regex + 容错)"""
        s = raw_text.strip()
        if not s:
            return {}

        # 1. Strip <think> ... </think> blocks (DeepSeek R1 style)
        if "<think>" in s and "</think>" in s:
            try:
                # Remove content between tags
                s = s.split("</think>")[-1].strip()
            except Exception:
                pass

        # 2. Try Regex for Markdown blocks (Standard LLM output)
        import re
        match = re.search(r"```json\s*(\{.*?\})\s*```", s, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass

        # 3. DeepSeek R1 structured format (REASONING/CONTENT)
        if "REASONING:" in s and "CONTENT:" in s:
            content_start = s.find("CONTENT:") + len("CONTENT:")
            content_part = s[content_start:].strip()
            if content_part:
                try:
                    i = content_part.find("{")
                    j = content_part.rfind("}")
                    if i >= 0 and j >= 0 and j > i:
                        return json.loads(content_part[i : j + 1])
                except Exception:
                    pass

        # 4. Fallback: Standard bracket finding (Greedy)
        i = s.find("{")
        j = s.rfind("}")
        if i >= 0 and j >= 0 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except Exception:
                pass

        # 5. Last resort: Return empty dict instead of raising
        return {}

    async def call_with_fallback(
        self,
        prompt: str,
        operation: str = "unknown"
    ) -> LLMCallResult:
        """
        调用 Provider 池，自动 Fallback

        Args:
            prompt: LLM prompt
            operation: 操作名称 (用于日志)

        Returns:
            LLMCallResult with detailed error tracking info

        Raises:
            RuntimeError: 所有 Provider 都失败 (已弃用，现在返回 LLMCallResult with success=False)
        """
        configs_to_use = self.configs
        if operation == "review_ml":
            raw = os.getenv("LLM_PROVIDERS_REVIEW")
            if raw:
                configs_to_use = self._build_configs_from_env(raw)
        if operation == "research_director":
            raw = os.getenv("LLM_PROVIDERS_RESEARCH")
            if raw:
                configs_to_use = self._build_configs_from_env(raw)
        ml_model = os.getenv("LLM_MODEL_ML")
        ml_ops = {"review_ml", "design_feature_specs", "suggest_feature_engineering", "plan_model_space", "plan_alpha"}
        if ml_model and operation in ml_ops:
            configs_to_use = [
                ProviderConfig(
                    name=c.name,
                    priority=c.priority,
                    timeout=c.timeout,
                    max_retries=c.max_retries,
                    model=ml_model if c.name == "ollama" else c.model,
                )
                for c in configs_to_use
            ]
        # Ensure stats placeholders for any new providers
        for c in configs_to_use:
            if c.name not in self.stats:
                self.stats[c.name] = {'calls': 0, 'successes': 0, 'failures': 0, 'timeouts': 0, 'fallbacks_from': 0}

        self.log.info(
            "fallback_call_start",
            operation=operation,
            total_providers=len(configs_to_use),
            priority_order=[c.name for c in configs_to_use]
        )

        last_error = None
        last_error_type = ""
        providers_attempted = []

        for i, config in enumerate(configs_to_use):
            provider_name = config.name
            providers_attempted.append(provider_name)
            is_fallback = i > 0

            if is_fallback:
                self.log.warning(
                    "falling_back_to_provider",
                    from_provider=self.configs[i-1].name,
                    to_provider=provider_name,
                    attempt_number=i + 1
                )
                self.stats[configs_to_use[i-1].name]['fallbacks_from'] += 1

            # 尝试调用 (with retries)
            for attempt in range(1, config.max_retries + 1):
                success, data, raw, error = await self._call_provider(
                    config, prompt, operation, attempt
                )

                if success:
                    self.log.info(
                        "fallback_call_success",
                        operation=operation,
                        provider=provider_name,
                        was_fallback=is_fallback,
                        total_attempts=i + 1
                    )

                    # 记录 Fallback 事件到文件
                    if self.log_failures and is_fallback:
                        self._log_fallback_event(
                            operation=operation,
                            failed_providers=[c.name for c in self.configs[:i]],
                            success_provider=provider_name,
                            attempt_number=i + 1
                        )

                    return LLMCallResult(
                        data=data,
                        raw=raw,
                        provider_used=provider_name,
                        success=True,
                        error_type="",
                        error_message="",
                        fallback_used=is_fallback,
                        providers_attempted=providers_attempted
                    )

                # 记录错误信息
                last_error = error
                if "timeout" in error.lower():
                    last_error_type = "timeout"
                elif "json" in error.lower() or "parse" in error.lower():
                    last_error_type = "json_parse_error"
                else:
                    last_error_type = "api_error"

                # 如果还有重试机会，等待一下
                if attempt < config.max_retries:
                    await asyncio.sleep(1.0 * attempt)  # 指数退避

        # 所有 Provider 都失败了
        self.log.error(
            "all_providers_failed",
            operation=operation,
            total_providers=len(self.configs),
            last_error=last_error
        )

        if self.log_failures:
            self._log_fallback_event(
                operation=operation,
                failed_providers=[c.name for c in self.configs],
                success_provider=None,
                attempt_number=len(self.configs),
                all_failed=True
            )

        # 返回失败结果 (不再抛出异常)
        return LLMCallResult(
            data={"error": "all_providers_failed"},
            raw="",
            provider_used="none",
            success=False,
            error_type="all_providers_failed",
            error_message=f"All {len(self.configs)} providers failed. Last error: {last_error}",
            fallback_used=True,
            providers_attempted=providers_attempted
        )

    def _log_fallback_event(
        self,
        operation: str,
        failed_providers: List[str],
        success_provider: Optional[str],
        attempt_number: int,
        all_failed: bool = False
    ):
        """记录 Fallback 事件到文件"""
        if not self.runs_dir:
            return

        try:
            from pathlib import Path
            d = Path(self.runs_dir) / datetime.now().date().isoformat()
            d.mkdir(parents=True, exist_ok=True)
            fp = d / "provider_fallback.jsonl"

            record = {
                "ts": datetime.now().isoformat(),
                "operation": operation,
                "failed_providers": failed_providers,
                "success_provider": success_provider,
                "attempt_number": attempt_number,
                "all_failed": all_failed,
            }

            with fp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        except Exception:
            pass

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "providers": self.stats,
            "configs": [
                {
                    "name": c.name,
                    "priority": c.priority,
                    "timeout": c.timeout,
                    "model": c.model or self._get_default_model(c.name)
                }
                for c in self.configs
            ]
        }

    def print_stats(self):
        """打印统计信息"""
        print("=" * 80)
        print("Provider Pool Statistics")
        print("=" * 80)
        print()

        total_calls = sum(s['calls'] for s in self.stats.values())

        print(f"{'Provider':<20} {'Calls':<8} {'Success':<8} {'Fail':<8} {'Timeout':<8} {'Fallback':<8} {'Success %'}")
        print("-" * 80)

        for config in self.configs:
            name = config.name
            s = self.stats[name]
            success_rate = (s['successes'] / s['calls'] * 100) if s['calls'] > 0 else 0

            print(
                f"{name:<20} {s['calls']:<8} {s['successes']:<8} {s['failures']:<8} "
                f"{s['timeouts']:<8} {s['fallbacks_from']:<8} {success_rate:.1f}%"
            )

        print()
        print(f"Total Calls: {total_calls}")
        print("=" * 80)
