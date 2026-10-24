from __future__ import annotations

"""
Simple Model Router to centralize LLM provider selection.
Current behavior: local-first (Ollama). Cloud providers can be added later.
"""

from scripts.ai_trader.brain.provider_fallback import ProviderConfig


class ModelRouter:
    def __init__(self, enable_cloud: bool = False):
        self.enable_cloud = enable_cloud

    def get_provider_config(self, task_type: str = "general", complexity: str = "low") -> ProviderConfig:
        # Cloud routing placeholder
        if self.enable_cloud and complexity == "high":
            # Extend here for cloud providers if desired
            pass

        # Local defaults
        if task_type == "coding":
            return ProviderConfig(name="ollama", model="qwen2.5:7b-instruct", priority=1, timeout=300)
        return ProviderConfig(name="ollama", model="deepseek-r1:1.5b", priority=1, timeout=300)
