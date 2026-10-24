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
        data_provider: Optional[Any] = None,
        portfolio_service: Optional[Any] = None,
        decision_bus: Optional[Any] = None,
        market_data_facade: Optional[Any] = None,
        price_cache: Optional[Any] = None,
        quote_staleness_sec: int = 5,
        poll_sec_1m: int = 3,
        poll_sec_5m: int = 10
    ):
        """
        Initialize strategy engine

        Args:
            specs: List of strategy specifications
            data_provider: Data access layer (ClickHouse, etc.)
            portfolio_service: Portfolio/position service
            decision_bus: Bus for submitting intents
            market_data_facade: MarketDataFacade instance for strategy data integration
            price_cache: PriceCache instance for low-latency L1 data
            quote_staleness_sec: Max age of L1 quotes in seconds
            poll_sec_1m: Polling interval for 1m bars
            poll_sec_5m: Polling interval for 5m bars
        """
        self.specs = {s.strategy_id: s for s in specs}
        self.data_provider = data_provider
        self.portfolio_service = portfolio_service
        self.decision_bus = decision_bus
        self.price_cache = price_cache

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
        """Stop strategy engine"""
        logger.info("=" * 80)
        logger.info("STRATEGY ENGINE STOPPING")
        logger.info("=" * 80)

        self._running = False

        # Stop polling tasks (strategy data integration)
        for task in self._poll_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Stop workers
        for strategy_id, task in self._workers.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Shutdown strategies
        for strategy_id, instance in self._instances.items():
            try:
                await instance.on_shutdown()
                logger.info(f"✅ Strategy shutdown: {strategy_id}")
            except Exception as e:
                logger.error(f"Error during shutdown: {strategy_id}", error=str(e))

        logger.info("=" * 80)
        logger.info("STRATEGY ENGINE STOPPED")
        logger.info("=" * 80)

    async def _load_strategy(self, strategy_id: str, spec: StrategySpec) -> None:
        """Load and instantiate strategy"""
        # Parse module path
        parts = spec.module_path.rsplit('.', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid module path: {spec.module_path}")

        module_name, class_name = parts

        # Import module
        module = importlib.import_module(module_name)

        # Get class
        strategy_class: Type[StrategyAlgorithm] = getattr(module, class_name)

        # Create context
        ctx = StrategyContext(
            engine=self,
            strategy_id=strategy_id,
            logger_func=logger,
            metrics_func=lambda: self._metrics[strategy_id],
            price_cache=self.price_cache
        )
        from strategies.stocks_kernel.assembly import legacy_kernel_assembly_provider

        setattr(ctx, "kernel_assembly_provider", legacy_kernel_assembly_provider)

        # Instantiate strategy
        instance = strategy_class(ctx, spec.params)

        # Store
        self._instances[strategy_id] = instance
        self._contexts[strategy_id] = ctx
        self._params[strategy_id] = spec.params
        self._cadences[strategy_id] = spec.params.cadence_s

        logger.info(f"Loaded strategy: {strategy_id} from {spec.module_path}")

    async def _strategy_worker(self, strategy_id: str) -> None:
        """
        Worker loop for strategy execution

        Runs at configured cadence and calls strategy callbacks
        """
        instance = self._instances[strategy_id]
        ctx = self._contexts[strategy_id]
        cadence_s = self._cadences.get(strategy_id, 5)

        logger.info(f"Worker started for {strategy_id} (cadence={cadence_s}s)")

        while self._running:
            try:
                start_time = time.time()

                # Get features for subscribed symbols
                if hasattr(ctx, '_subscriptions'):
                    for symbol, resolutions in ctx._subscriptions.items():
                        for bar_sec in resolutions:
                            # Fetch latest features
                            features = await self._get_latest_features(symbol, bar_sec)

                            if features:
                                # Call strategy
                                await instance.on_features(symbol, features)

                # Record metrics
                latency_ms = (time.time() - start_time) * 1000
                self._metrics[strategy_id]['ticks'] += 1
                self._metrics[strategy_id]['avg_latency_ms'] = (
                    0.9 * self._metrics[strategy_id]['avg_latency_ms'] +
                    0.1 * latency_ms
                )

                # Sleep until next cadence
                await asyncio.sleep(cadence_s)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error for {strategy_id}", error=str(e))
                self._metrics[strategy_id]['errors'] += 1
                await asyncio.sleep(1)

        logger.info(f"Worker stopped for {strategy_id}")

    async def _get_latest_features(
        self,
        symbol: str,
        bar_sec: int
    ) -> Optional[Dict[str, Any]]:
        """Get latest features for symbol"""
        if not self.data_provider:
            return None

        try:
            # Fetch last feature row
            features = await self.data_provider.get_latest_features(symbol, bar_sec)
            return features
        except Exception as e:
            logger.error(f"Failed to get features for {symbol}", error=str(e))
            return None

    # ============================================================================
    # Context API Implementation (called by strategies via StrategyContext)
    # ============================================================================

    async def subscribe_features(
        self,
        strategy_id: str,
        symbols: List[str],
        bar_sec: int
    ) -> None:
        """Subscribe to features (called by strategy context)"""
        logger.info(f"Strategy {strategy_id} subscribed to {symbols} @ {bar_sec}s")

    async def history_features(
        self,
        symbol: str,
        bar_sec: int,
        start_ts: Optional[int],
        end_ts: Optional[int],
        fields: Optional[List[str]],
        limit: int
    ) -> List[Dict[str, Any]]:
        """Fetch historical features"""
        if not self.data_provider:
            return []

        try:
            return await self.data_provider.get_history(
                symbol, bar_sec, start_ts, end_ts, fields, limit
            )
        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}", error=str(e))
            return []

    async def set_cadence(self, strategy_id: str, seconds: int) -> None:
        """Set strategy cadence"""
        if seconds < 5:
            logger.warning(f"Cadence too low: {seconds}s, using minimum 5s")
            seconds = 5

        self._cadences[strategy_id] = seconds
        logger.info(f"Cadence set for {strategy_id}: {seconds}s")

    async def portfolio_view(self, strategy_id: str) -> Dict[str, Any]:
        """Get portfolio snapshot"""
        if not self.portfolio_service:
            return {'cash': 0.0, 'equity': 0.0, 'positions': []}

        try:
            return await self.portfolio_service.get_portfolio(strategy_id)
        except Exception as e:
            logger.error(f"Failed to get portfolio for {strategy_id}", error=str(e))
            return {'cash': 0.0, 'equity': 0.0, 'positions': []}

    async def position_view(self, strategy_id: str, symbol: str) -> Dict[str, Any]:
        """Get position for symbol"""
        if not self.portfolio_service:
            return {'symbol': symbol, 'qty': 0.0, 'avg_px': 0.0}

        try:
            return await self.portfolio_service.get_position(strategy_id, symbol)
        except Exception as e:
            logger.error(f"Failed to get position for {strategy_id}/{symbol}", error=str(e))
            return {'symbol': symbol, 'qty': 0.0, 'avg_px': 0.0}

    async def submit_intent(
        self,
        strategy_id: str,
        envelope: DecisionEnvelope
    ) -> Any:
        """
        Submit trading intent to decision pipeline

        Args:
            strategy_id: Strategy ID
            envelope: Decision envelope
        """
        if not self.decision_bus:
            logger.warning(f"No decision bus configured, intent not submitted: {strategy_id}")
            return {"status": "UNKNOWN", "reasons": ["decision_bus_not_configured"]}

        try:
            # Publish to decision pipeline
            await self.decision_bus.publish(
                topic='engine.decision.v1',
                key=f"{strategy_id}:{envelope.symbol}",
                value=envelope.to_dict()
            )

            self._metrics[strategy_id]['decisions'] += 1

            logger.info(
                f"Intent submitted: {strategy_id}",
                symbol=envelope.symbol,
                target_qty=envelope.target_qty,
                decision_id=envelope.decision_id
            )
            return {"status": "APPROVED", "reasons": ["published_to_decision_bus"]}

        except Exception as e:
            logger.error(f"Failed to submit intent: {strategy_id}", error=str(e))
            return {"status": "ERROR", "reasons": [str(e)]}

    def get_param(self, strategy_id: str, key: str, default=None) -> Any:
        """Get strategy parameter"""
        params = self._params.get(strategy_id)
        if not params:
            return default

        # Check params object
        if hasattr(params, key):
            return getattr(params, key)

        # Check extras dict
        if params.extras and key in params.extras:
            return params.extras[key]

        return default

    def get_metrics(self, strategy_id: Optional[str] = None) -> Dict[str, Any]:
        """Get metrics for strategy or all strategies"""
        if strategy_id:
            return dict(self._metrics.get(strategy_id, {}))

        return {sid: dict(metrics) for sid, metrics in self._metrics.items()}

    # ============================================================================
    # Strategy Data Integration (doc/strategy/1.md)
    # ============================================================================

    async def history_features_unified(
        self,
        symbol: str,
        bar_type: str,
        limit: int
    ) -> List[Dict[str, Any]]:
        """
        Get historical features for strategy initialization.
        Maps bar_type to resolution and handles VIX aliasing.

        Args:
            symbol: Symbol (TSLA, SPY, VIX, etc.)
            bar_type: Bar type ('1m', '5m', '60', '300', etc.)
            limit: Number of bars to fetch

        Returns:
            List of feature dicts, sorted ascending (oldest to newest)
        """
        if not self.md:
            logger.warning("MarketDataFacade not configured, returning empty history")
            return []

        # Map bar_type to resolution
        res_map = {
            "60": "1m",
            "300": "5m",
            "1m": "1m",
            "5m": "5m"
        }
        res = res_map.get(str(bar_type), str(bar_type))

        # Handle VIX aliasing
        sym = self.md.vix_symbol() if symbol == "VIX" else symbol

        try:
            return self.md.bars(sym, res, limit)
        except Exception as e:
            logger.error(f"Failed to fetch history", error=str(e), symbol=symbol, res=res)
            return []

    async def subscribe_features_unified(
        self,
        strategy_id: str,
        symbols: List[str],
        bar_sec: int
    ) -> None:
        """
        Subscribe to features using poll-based approach.
        Polls ClickHouse for new bars and triggers strategy callbacks.

        Args:
            strategy_id: Strategy ID
            symbols: List of symbols to subscribe
            bar_sec: Bar resolution in seconds (60 for 1m, 300 for 5m)
        """
        if not self.md:
            logger.warning("MarketDataFacade not configured, cannot subscribe")
            return

        # Determine resolution and poll interval
        res = "1m" if bar_sec <= 60 else "5m"
        poll_sec = self.poll_sec_1m if res == "1m" else self.poll_sec_5m

        logger.info(f"Subscribing {strategy_id} to {symbols} @ {res} (poll every {poll_sec}s)")

        # Create polling task for each symbol
        for symbol in symbols:
            # Handle VIX aliasing
            actual_symbol = self.md.vix_symbol() if symbol == "VIX" else symbol

            # Create callback for this symbol
            async def on_new_bar(bar_row: Dict[str, Any], sym=actual_symbol, orig_sym=symbol):
                """Callback when new bar is detected"""
                try:
                    # Build feature dict from bar
                    features = dict(bar_row)

                    # For trading symbols (not SPY/QQQ/VIX/VIXY), add L1 quotes
                    if orig_sym not in ("SPY", "QQQ", "VIX", "VIXY"):
                        quote = self.md.last_quote(sym, staleness_sec=self.quote_staleness_sec)
                        features.update(quote)

                    # Get strategy instance and call on_features
                    instance = self._instances.get(strategy_id)
                    if instance:
                        await instance.on_features(orig_sym, features)

                except Exception as e:
                    logger.error(f"Error in bar callback", error=str(e), symbol=orig_sym, strategy=strategy_id)

            # Start polling task
            task = asyncio.create_task(
                self.md.poll_new_bar(actual_symbol, res, poll_sec, on_new_bar)
            )
            self._poll_tasks.append(task)

        logger.info(f"✅ Subscribed {strategy_id} to {len(symbols)} symbols")


# Data provider interface (to be implemented with ClickHouse)
class DataProvider:
    """Abstract data provider interface"""

    async def get_latest_features(
        self,
        symbol: str,
        bar_sec: int
    ) -> Optional[Dict[str, Any]]:
        """Get latest feature row"""
        raise NotImplementedError

    async def get_history(
        self,
        symbol: str,
        bar_sec: int,
        start_ts: Optional[int],
        end_ts: Optional[int],
        fields: Optional[List[str]],
        limit: int
    ) -> List[Dict[str, Any]]:
        """Get historical features"""
        raise NotImplementedError
