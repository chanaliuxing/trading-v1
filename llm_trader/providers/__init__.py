"""LLM Provider abstraction layer"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum


class LLMProvider(str, Enum):
    """Supported LLM providers"""
    OPENAI = "openai"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    DEEPSEEK_NVIDIA = "deepseek_nvidia"


@dataclass
class LLMResponse:
    """Standardized LLM response (intraday-ready)

    Extended with optional planning fields so the agent can pre-place orders
    without chasing: limit price, take-profit/stop in bps, and a time window.
    """
    decision: str  # BUY, SELL, HOLD
    symbol: str
    quantity: Optional[int] = None
    confidence: Optional[float] = None  # 0-1
    reasoning: Optional[str] = None
    raw_response: Optional[str] = None
    provider_name: Optional[str] = None  # Added provider_name
    # Optional planning fields
    limit_price: Optional[float] = None          # Suggested limit price
    take_profit_bps: Optional[int] = None        # TP in basis points
    stop_loss_bps: Optional[int] = None          # SL in basis points
    time_window_s: Optional[int] = None          # Valid window for the plan


class BaseLLMProvider(ABC):
    """Base class for all LLM providers"""

    def __init__(self, api_key: str, model: str, **kwargs):
        self.api_key = api_key
        self.model = model
        self.extra_params = kwargs
        # Optional name for logging/telemetry
        self.name = kwargs.get("name", self.__class__.__name__.lower())

    @abstractmethod
    async def analyze_market(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
    ) -> LLMResponse:
        """Analyze market data and return trading decision

        Args:
            symbol: Stock symbol to analyze
            market_data: Dict containing:
                - current_price: float
                - features_5s: List[Dict] - recent 5s features
                - features_1m: List[Dict] - recent 1m features
                - sfs_features: Dict - sliding feature service data
                - volume: float
                - etc.
            portfolio_state: Dict containing:
                - cash: float
                - positions: Dict[symbol, quantity]
                - total_value: float
                - max_position_size: float

        Returns:
            LLMResponse with trading decision
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the LLM provider is accessible"""
        pass

    async def analyze_market_with_prompt(
        self,
        symbol: str,
        custom_prompt: str,
        portfolio_state: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> LLMResponse:
        """Analyze market with a custom V2 prompt (optional for V2 support)

        Args:
            symbol: Stock symbol
            custom_prompt: Pre-built prompt from PromptBuilder

        Returns:
            LLMResponse with trading decision
        """
        # Default implementation: parse the prompt and call analyze_market
        # Providers can override this for better custom prompt support
        raise NotImplementedError(
            "This provider does not support custom prompts. "
            "Use V1 mode or implement analyze_market_with_prompt."
        )

    async def generate_response(self, prompt: str) -> str:
        """Generate a text response from the LLM (for reflections, feedback, etc.)

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            Raw text response from the LLM
        """
        # Default implementation - providers should override
        raise NotImplementedError(
            "This provider does not support generate_response. "
            "Implement this method for V2 reflection support."
        )
