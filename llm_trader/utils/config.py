"""Configuration loader from .env file"""

import os
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv


class Config:
    """Configuration loaded from environment variables"""

    def __init__(self, env_file: Optional[str] = None):
        """Load configuration from .env file

        Args:
            env_file: Path to .env file (default: searches parent directories)
        """
        if env_file:
            load_dotenv(env_file)
        else:
            # Try to find .env in parent directory
            current_dir = os.path.dirname(os.path.abspath(__file__))
            parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
            env_path = os.path.join(parent_dir, ".env")
            if os.path.exists(env_path):
                load_dotenv(env_path)
                print(f"Loaded .env from {env_path}")
            else:
                print("Warning: .env file not found")

        # ClickHouse config
        self.ch_host = os.getenv("CH_HOST", "localhost")
        self.ch_port = int(os.getenv("CH_PORT", "8123"))
        self.ch_user = os.getenv("CH_USER", "default")
        self.ch_password = os.getenv("CH_PASSWORD", "")
        self.ch_database = os.getenv("CH_DB", "trading")

        # Redis config
        self.redis_url = os.getenv("STATEBUS_REDIS", "redis://127.0.0.1:6379/0")

        # IBKR config
        self.ib_host = os.getenv("IB_HOST", "127.0.0.1")
        self.ib_port = int(os.getenv("IB_PAPER_PORT", "4002"))
        self.ib_client_id = int(os.getenv("IB_CLIENT_ID", "100"))

        # LLM API keys
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", "")

        # Trading symbols from config.yml universe
        self.trading_symbols = self._load_universe()
        print(f"Loaded {len(self.trading_symbols)} trading symbols from universe")

    def get_llm_config(self, provider: str) -> Dict[str, Any]:
        """Get LLM provider configuration

        Args:
            provider: LLM provider name (openai, gemini, deepseek, deepseek_nvidia)

        Returns:
            Dict with api_key and model
        """
        configs = {
            "openai": {
                "api_key": self.openai_api_key,
                "model": "gpt-4o-mini",  # Fast reasoning model
            },
            "gemini": {
                "api_key": self.gemini_api_key,
                "model": "gemini-2.0-flash-exp",  # Fast experimental model
            },
            "deepseek": {
                "api_key": self.deepseek_api_key,
                "model": "deepseek-chat",  # DeepSeek V3 (fast)
            },
            "deepseek_nvidia": {
                "api_key": self.nvidia_api_key,
                "model": "deepseek-ai/deepseek-r1",  # DeepSeek R1 (reasoning, via NVIDIA)
            },
            "ollama": {
                "api_key": "not-needed",  # Ollama doesn't need API key
                "model": os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct"),
                "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            },
        }

        if provider not in configs:
            raise ValueError(f"Unknown provider: {provider}")

        config = configs[provider]
        # Skip API key check for Ollama
        if provider != "ollama" and not config["api_key"]:
            raise ValueError(f"API key not found for {provider}")

        return config

    def _load_universe(self) -> List[str]:
        """Load trading universe from config.yml

        Returns:
            List of trading symbols from universe section
        """
        # Find config.yml in project root
        current_dir = Path(__file__).resolve()
        project_root = current_dir.parent.parent.parent  # llm_trader/llm_trader/utils -> llm_trader
        config_path = project_root / "config.yml"

        if not config_path.exists():
            # Try parent directory (trading/)
            config_path = project_root.parent / "config.yml"

        if not config_path.exists():
            print(f"Warning: config.yml not found, using fallback symbols")
            return ["AAPL", "TSLA", "NVDA"]

        try:
            with open(config_path, 'r') as f:
                config_data = yaml.safe_load(f)

            universe = config_data.get("universe", [])
            if not universe:
                print(f"Warning: Empty universe in config.yml, using fallback")
                return ["AAPL", "TSLA", "NVDA"]

            return universe
        except Exception as e:
            print(f"Error loading config.yml: {e}, using fallback symbols")
            return ["AAPL", "TSLA", "NVDA"]
