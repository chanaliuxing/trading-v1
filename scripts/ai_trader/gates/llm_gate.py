import importlib
import os
import asyncio


ENV_KEYS = {
    "deepseek_nvidia": "NVIDIA_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


PROV_CFG = {
    "openai": {
        "module": "openai_provider",
        "cls": "OpenAIProvider",
        "env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "gemini": {
        "module": "gemini_provider",
        "cls": "GeminiProvider",
        "env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",
    },
    "deepseek": {
        "module": "deepseek_provider",
        "cls": "DeepSeekProvider",
        "env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "deepseek_nvidia": {
        "module": "deepseek_nvidia_provider",
        "cls": "DeepSeekNvidiaProvider",
        "env": "NVIDIA_API_KEY",
        "model": "deepseek-ai/deepseek-r1",
    },
}


class LLMGate:
    """Lightweight LLM provider readiness gate.

    - Verifies provider modules importable from llm_trader.llm_trader.providers
    - Verifies required API keys exist in environment
    - Optional deep check may perform a tiny request (disabled by default)
    """

    def __init__(self, providers=("deepseek_nvidia", "deepseek", "openai", "gemini"), deep_check: bool = False, deep_check_providers=None):
        self.providers = providers
        self.deep_check = deep_check
        self.deep_check_providers = set(deep_check_providers or ("openai", "gemini", "deepseek"))

    def check(self) -> dict:
        ok = True
        issues = []
        results = {}
        for p in self.providers:
            # 1) API key
            envk = PROV_CFG.get(p, {}).get("env")
            has_key = bool(envk and os.getenv(envk))
            if not has_key:
                ok = False
                issues.append(f"missing_env:{p}:{envk}")

            # 2) Import provider module
            modname = f"llm_trader.llm_trader.providers.{PROV_CFG.get(p,{}).get('module', p+'_provider')}"
            try:
                importlib.import_module(modname)
                imported = True
            except Exception as e:
                imported = False
                ok = False
                issues.append(f"import_failed:{p}:{e}")

            results[p] = {"api_key": has_key, "imported": imported}

            # 3) Deep check: optional, disabled by default to avoid costs
            if self.deep_check and has_key and imported and (p in self.deep_check_providers):
                try:
                    mod = importlib.import_module(modname)
                    cls_name = PROV_CFG[p]["cls"]
                    cls = getattr(mod, cls_name)
                    prov = cls(os.getenv(envk), PROV_CFG[p]["model"])  # minimal ctor
                    # Call async health_check with short timeout by using asyncio.wait_for
                    async def _hc():
                        return await prov.health_check()
                    res = asyncio.run(asyncio.wait_for(_hc(), timeout=15.0))
                    results[p]["deep"] = bool(res)
                    if not res:
                        ok = False
                        issues.append(f"deep_check_failed:{p}")
                except Exception as e:
                    ok = False
                    results[p]["deep"] = False
                    issues.append(f"deep_check_error:{p}:{e}")

        return {"ok": ok, "results": results, "issues": issues}
