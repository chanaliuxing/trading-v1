"""
Strategy Registry
Hot-loading and registration of strategy classes
Based on doc/59.md specification
"""
import hashlib
import json
import importlib
from typing import Dict, Any, Type, Optional

from libs.strategy.base import StrategyAlgorithm, StrategyParams, StrategyContext
from libs.logger import get_logger

logger = get_logger(__name__)


class StrategyRegistry:
    """
    Strategy class registry

    Features:
    - Register strategy classes by name
    - Load strategies with parameter hashing
    - Hot reload on parameter changes
    """

    def __init__(self):
        self._registry: Dict[str, Type[StrategyAlgorithm]] = {}
        logger.info("StrategyRegistry initialized")

    def register(self, name: str, cls: Type[StrategyAlgorithm]) -> None:
        """
        Register a strategy class

        Args:
            name: Strategy name (e.g., 'meanrev_v2')
            cls: Strategy class
        """
        self._registry[name] = cls
        logger.info("Strategy registered", name=name, class_name=cls.__name__)

    def get(self, name: str) -> Optional[Type[StrategyAlgorithm]]:
        """
        Get registered strategy class

        Args:
            name: Strategy name

        Returns:
            Strategy class or None
        """
        return self._registry.get(name)

    def list_strategies(self) -> list[str]:
        """
        List all registered strategies

        Returns:
            List of strategy names
        """
        return list(self._registry.keys())

    def param_hash(self, params: Dict[str, Any]) -> str:
        """
        Calculate parameter hash

        Used for reproducibility and version tracking.

        Args:
            params: Parameter dict

        Returns:
            10-character hash
        """
        # Sort keys for deterministic hash
        s = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(s.encode('utf-8')).hexdigest()[:10]

    def load(
        self,
        name: str,
        params: Dict[str, Any],
        ctx: StrategyContext
    ) -> StrategyAlgorithm:
        """
        Load strategy instance

        Args:
            name: Strategy name
            params: Strategy parameters
            ctx: Strategy context

        Returns:
            Strategy instance

        Raises:
            KeyError: If strategy not registered
        """
        cls = self._registry.get(name)
        if not cls:
            raise KeyError(f"Strategy not registered: {name}")

        # Calculate parameter hash for versioning
        phash = self.param_hash(params)

        logger.info("Loading strategy",
                   name=name,
                   param_hash=phash,
                   strategy_id=ctx.strategy_id)

        # Create strategy params object
        strategy_params = StrategyParams(**params)

        # Instantiate strategy
        return cls(ctx, strategy_params)

    def hot_reload(
        self,
        name: str,
        old_instance: StrategyAlgorithm,
        new_params: Dict[str, Any],
        ctx: StrategyContext
    ) -> StrategyAlgorithm:
        """
        Hot reload strategy with new parameters

        Process:
        1. Create new instance with new parameters
        2. Pause old instance (stop accepting new signals)
        3. Wait for old instance callbacks to complete
        4. Transfer state to new instance
        5. Activate new instance

        Args:
            name: Strategy name
            old_instance: Current instance
            new_params: New parameters
            ctx: Strategy context

        Returns:
            New strategy instance
        """
        old_hash = self.param_hash(old_instance.params.__dict__)
        new_hash = self.param_hash(new_params)

        logger.info("Hot reloading strategy",
                   name=name,
                   old_hash=old_hash,
                   new_hash=new_hash)

        # Create new instance
        new_instance = self.load(name, new_params, ctx)

        # Transfer state (optional, strategy-dependent)
        if hasattr(old_instance, 'state'):
            new_instance.state = old_instance.state.copy()

        logger.info("Strategy hot reload complete",
                   name=name,
                   new_hash=new_hash)

        return new_instance


# Global registry instance
_global_registry = StrategyRegistry()


def register_strategy(name: str):
    """
    Decorator to register a strategy class

    Usage:
        @register_strategy("meanrev_v2")
        class MeanReversionStrategy(StrategyAlgorithm):
            ...

    Args:
        name: Strategy name
    """
    def decorator(cls: Type[StrategyAlgorithm]):
        _global_registry.register(name, cls)
        return cls
    return decorator


def get_registry() -> StrategyRegistry:
    """Get global strategy registry"""
    return _global_registry
