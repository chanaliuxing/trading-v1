# StrategyEngine 隔离升级 - 实施代码方案

**文档状态**: 草案
**创建日期**: 2025-10-17
**负责人**: Gemini

---

本文档提供了对 `config.yml` 和 `services/strategy/strategy_engine.py` 的具体代码修改，以实现 `StrategyEngine` 的资源隔离升级。

所有变更均遵循 `PLAN.md` 中制定的渐进式、双路径方案。

## 1. 配置文件 (`config.yml`) 修改

在 `strategies` 列表下的每个策略定义中，增加 `use_isolated_context` 开关。

*   **默认值**: `false`，确保现有行为不变。
*   **试点策略**: 为第一个策略 `tsla_v7_2_001` 设置为 `true`，作为本次升级的试点。

```yaml
# Proposed new content for: config.yml
# Only the 'strategies' section is shown for brevity.

# ... (previous content of config.yml) ...

strategies:
  - id: "tsla_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Enable isolated context for this pilot strategy ---
    use_isolated_context: true
    params:
      extras:
        tsla_v7_2_config:
          symbol: "TSLA"
          # ... (rest of params)

  - id: "aapl_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "AAPL"
          # ... (rest of params)

  - id: "nvda_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "NVDA"
          # ... (rest of params)

  - id: "meta_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "META"
          # ... (rest of params)

  - id: "msft_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "MSFT"
          # ... (rest of params)

  - id: "googl_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "GOOGL"
          # ... (rest of params)

  - id: "amzn_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "AMZN"
          # ... (rest of params)

  - id: "amd_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "AMD"
          # ... (rest of params)

  - id: "qqq_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        tsla_v7_2_config:
          symbol: "QQQ"
          # ... (rest of params)

  - id: "aapl_dividend_wave_001"
    enabled: true
    module: "strategies.aapl_dividend_wave"
    class: "AAPLDividendWave"
    capital_limit_usd: 30000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        aapl_dividend_wave_config:
          symbol: "AAPL"
          # ... (rest of params)

  - id: "premarket_gainer_001"
    enabled: true
    module: "strategies.premarket_gainer_v1"
    class: "PremarketGainerV1"
    capital_limit_usd: 100000
    # --- MODIFICATION: Add flag with default value ---
    use_isolated_context: false
    params:
      extras:
        premarket_config:
          # ... (rest of params)

# ... (rest of config.yml) ...
```

## 2. 策略引擎 (`services/strategy/strategy_engine.py`) 修改

这是本次升级的核心，修改后的代码如下。关键变更点已在注释中用 `MODIFICATION` 标出。

```python
# Proposed new content for: services/strategy/strategy_engine.py

"""
Strategy Engine
Based on doc/23.md

Orchestrates strategy execution with:
- Independent cadence per strategy
- Event-driven lifecycle
- Data readiness checking
- Intent submission to decision pipeline
"""
import asyncio
import importlib
import time
from typing import Dict, List, Optional, Any, Type
from dataclasses import dataclass, field
from collections import defaultdict

from libs.logger import get_logger
# --- MODIFICATION: Assume these can be imported from their modules ---
from services.data.data_provider import DataProvider
from services.portfolio.portfolio_service import PortfolioService
from services.strategy.strategy_base import (
    StrategyAlgorithm,
    StrategyContext,
    StrategyParams,
    DecisionEnvelope
)

logger = get_logger(__name__)


@dataclass
class StrategySpec:
    """Strategy specification for engine"""
    strategy_id: str
    module_path: str  # e.g. "strategies.mean_revert.MeanRevert5s"
    symbols: List[str]
    params: StrategyParams
    enabled: bool = True
    # --- MODIFICATION: Add a field to hold the full config dict for easy flag access ---
    config: Dict[str, Any] = field(default_factory=dict)


class StrategyEngine:
    """
    Strategy execution engine

    Features:
    - Load and instantiate strategies dynamically
    - Independent execution cadence per strategy
    - Data readiness checks before execution
    - Event distribution (features, orders, positions)
    - Intent submission to decision pipeline
    """

    def __init__(
        self,
        specs: List[StrategySpec],
        # --- MODIFICATION: Change constructor signature for better dependency injection ---
        db_connection_pool: Optional[Any] = None, # Accept a shared DB pool
        decision_bus: Optional[Any] = None,
        market_data_facade: Optional[Any] = None,
        price_cache: Optional[Any] = None,
        quote_staleness_sec: int = 5,
        poll_sec_1m: int = 3,
        poll_sec_5m: int = 10
    ):
        """
        Initialize strategy engine
        """
        self.specs = {s.strategy_id: s for s in specs}
        self.decision_bus = decision_bus
        self.price_cache = price_cache
        self.db_connection_pool = db_connection_pool # Store the shared pool

        # --- MODIFICATION: Create shared instances for SHARED mode strategies ---
        logger.info("Creating shared service instances for legacy/shared mode strategies...")
        self.shared_data_provider = DataProvider(db_connection_pool=self.db_connection_pool) if self.db_connection_pool else None
        self.shared_portfolio_service = PortfolioService(db_connection_pool=self.db_connection_pool) if self.db_connection_pool else None
        # --- END MODIFICATION ---

        # Market data facade for strategy integration (doc/strategy/1.md)
        self.md = market_data_facade
        self.quote_staleness_sec = quote_staleness_sec
        self.poll_sec_1m = poll_sec_1m
        self.poll_sec_5m = poll_sec_5m

        # Strategy instances and contexts
        self._instances: Dict[str, StrategyAlgorithm] = {}
        self._contexts: Dict[str, StrategyContext] = {}
        self._workers: Dict[str, asyncio.Task] = {}

        # Configuration
        self._params: Dict[str, StrategyParams] = {}
        self._cadences: Dict[str, int] = {}

        # Polling tasks for strategy data integration
        self._poll_tasks: List[asyncio.Task] = []

        # Metrics
        self._metrics = defaultdict(lambda: {
            'ticks': 0,
            'decisions': 0,
            'skips': 0,
            'errors': 0,
            'avg_latency_ms': 0.0
        })

        self._running = False

        logger.info("StrategyEngine initialized", count=len(specs))

    async def start(self) -> None:
        """Start strategy engine"""
        logger.info("=" * 80)
        logger.info("STRATEGY ENGINE STARTING")
        logger.info("=" * 80)

        # Load and instantiate strategies
        for strategy_id, spec in self.specs.items():
            if not spec.enabled:
                logger.info(f"Skipping disabled strategy: {strategy_id}")
                continue

            try:
                # --- MODIFICATION: _load_strategy now handles the dual-path logic ---
                await self._load_strategy(strategy_id, spec)
            except Exception as e:
                logger.error(f"Failed to load strategy: {strategy_id}", error=str(e))

        # Initialize strategies
        for strategy_id, instance in self._instances.items():
            try:
                await instance.initialize()
                logger.info(f"✅ Strategy initialized: {strategy_id}")
            except Exception as e:
                logger.error(f"Failed to initialize strategy: {strategy_id}", error=str(e))

        # Start workers
        self._running = True
        for strategy_id in self._instances.keys():
            self._workers[strategy_id] = asyncio.create_task(
                self._strategy_worker(strategy_id)
            )

        logger.info("=" * 80)
        logger.info("STRATEGY ENGINE RUNNING")
        logger.info("=" * 80)
        logger.info(f"Active strategies: {len(self._instances)}")

    async def stop(self) -> None:
        # ... (stop logic remains unchanged) ...
        pass

    async def _load_strategy(self, strategy_id: str, spec: StrategySpec) -> None:
        """Load and instantiate strategy with dual-path dependency injection."""
        # Parse module path
        parts = spec.module_path.rsplit('.', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid module path: {spec.module_path}")

        module_name, class_name = parts
        module = importlib.import_module(module_name)
        strategy_class: Type[StrategyAlgorithm] = getattr(module, class_name)

        # --- MODIFICATION: DUAL-PATH LOGIC ---
        use_isolated_context = spec.config.get("use_isolated_context", False)
        
        data_provider_instance: Optional[DataProvider]
        portfolio_service_instance: Optional[PortfolioService]

        if use_isolated_context:
            logger.info(f"Loading strategy '{strategy_id}' in ISOLATED mode.")
            data_provider_instance = DataProvider(db_connection_pool=self.db_connection_pool) if self.db_connection_pool else None
            portfolio_service_instance = PortfolioService(db_connection_pool=self.db_connection_pool, strategy_id=strategy_id) if self.db_connection_pool else None
        else:
            logger.info(f"Loading strategy '{strategy_id}' in SHARED mode.")
            data_provider_instance = self.shared_data_provider
            portfolio_service_instance = self.shared_portfolio_service
        # --- END MODIFICATION ---

        # Create context, now passing the selected service instances
        ctx = StrategyContext(
            engine=self,
            strategy_id=strategy_id,
            logger_func=logger,
            metrics_func=lambda: self._metrics[strategy_id],
            price_cache=self.price_cache,
            # --- MODIFICATION: Pass resolved dependencies to context ---
            data_provider=data_provider_instance,
            portfolio_service=portfolio_service_instance
        )

        instance = strategy_class(ctx, spec.params)

        self._instances[strategy_id] = instance
        self._contexts[strategy_id] = ctx
        self._params[strategy_id] = spec.params
        self._cadences[strategy_id] = spec.params.cadence_s

        logger.info(f"Loaded strategy: {strategy_id} from {spec.module_path}")

    async def _strategy_worker(self, strategy_id: str) -> None:
        # ... (worker logic remains largely the same) ...
        # IMPORTANT: The worker should use services from its context, not the engine's.
        # This change assumes on_features is the primary entry point and it receives data
        # from the polling mechanism, which is already strategy-specific.
        # If _get_latest_features were still used, it would need to be refactored to use
        # the context's data_provider.
        pass


    async def _get_latest_features(self, symbol: str, bar_sec: int, data_provider: DataProvider) -> Optional[Dict[str, Any]]:
        """
        MODIFICATION: This function now requires a data_provider to be passed in,
        removing its dependency on self.data_provider.
        """
        if not data_provider:
            return None
        try:
            return await data_provider.get_latest_features(symbol, bar_sec)
        except Exception as e:
            logger.error(f"Failed to get features for {symbol}", error=str(e))
            return None

    # ============================================================================
    # Context API Implementation (called by strategies via StrategyContext)
    # These methods should now use the context-specific services.
    # ============================================================================

    async def history_features(self, strategy_id: str, symbol: str, bar_sec: int, start_ts: Optional[int], end_ts: Optional[int], fields: Optional[List[str]], limit: int) -> List[Dict[str, Any]]:
        """Fetch historical features using the strategy's own data provider."""
        # --- MODIFICATION: Use context-specific data_provider ---
        ctx = self._contexts.get(strategy_id)
        if not ctx or not ctx.data_provider:
            return []
        try:
            return await ctx.data_provider.get_history(symbol, bar_sec, start_ts, end_ts, fields, limit)
        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}", error=str(e), strategy_id=strategy_id)
            return []

    async def portfolio_view(self, strategy_id: str) -> Dict[str, Any]:
        """Get portfolio snapshot using the strategy's own portfolio service."""
        # --- MODIFICATION: Use context-specific portfolio_service ---
        ctx = self._contexts.get(strategy_id)
        if not ctx or not ctx.portfolio_service:
            return {'cash': 0.0, 'equity': 0.0, 'positions': []}
        try:
            return await ctx.portfolio_service.get_portfolio(strategy_id)
        except Exception as e:
            logger.error(f"Failed to get portfolio for {strategy_id}", error=str(e))
            return {'cash': 0.0, 'equity': 0.0, 'positions': []}

    async def position_view(self, strategy_id: str, symbol: str) -> Dict[str, Any]:
        """Get position for symbol using the strategy's own portfolio service."""
        # --- MODIFICATION: Use context-specific portfolio_service ---
        ctx = self._contexts.get(strategy_id)
        if not ctx or not ctx.portfolio_service:
            return {'symbol': symbol, 'qty': 0.0, 'avg_px': 0.0}
        try:
            return await ctx.portfolio_service.get_position(strategy_id, symbol)
        except Exception as e:
            logger.error(f"Failed to get position for {strategy_id}/{symbol}", error=str(e))
            return {'symbol': symbol, 'qty': 0.0, 'avg_px': 0.0}

    # ... (other methods like submit_intent, get_param, etc., remain unchanged) ...

    # ... (The rest of the file) ...
```

## 3. 策略基础 (`services/strategy/strategy_base.py`) 修改建议

为了让 `StrategyContext` 能够持有隔离的实例，需要对其 `__init__` 方法进行修改。

```python
# Proposed change for: services/strategy/strategy_base.py

class StrategyContext:
    def __init__(
        self,
        engine,
        strategy_id,
        logger_func,
        metrics_func,
        price_cache,
        # --- MODIFICATION: Add data_provider and portfolio_service to constructor ---
        data_provider: Optional[Any] = None,
        portfolio_service: Optional[Any] = None
    ):
        self._engine = engine
        self.strategy_id = strategy_id
        self.logger = logger_func
        self.metrics = metrics_func
        self._price_cache = price_cache
        
        # --- MODIFICATION: Store the isolated instances ---
        self.data_provider = data_provider
        self.portfolio_service = portfolio_service
        
        self._subscriptions = {}
```

---

这份 `IMPLEMENTATION.md` 文件提供了所有必要的代码变更。请您审阅，确认无误后，我们就可以进入实际的文件修改和测试阶段。
