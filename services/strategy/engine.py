"""
Strategy Engine
Manages strategy lifecycle and execution
Based on doc/8.md and doc/59.md specifications

Features:
- Independent cadence per strategy
- Event-driven execution (IoC pattern)
- Data readiness checks
- Error handling and circuit breaking
"""
import asyncio
import time
import os
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from libs.strategy import (
    StrategyAlgorithm,
    StrategyContext,
    StrategyParams,
    DecisionEnvelope,
    PositionSnapshot,
    OrderUpdate
)
from libs.strategy.registry import get_registry
from libs.db.ch_pool import get_client
from libs.logger import get_logger
from prometheus_client import Counter, Histogram
from inference import InferenceManager
from services.health import SystemHealthController
from services.orders.truth import OrderTruthService

logger = get_logger(__name__)

# Metrics
strategy_executions = Counter(
    'strategy_executions_total',
    'Total strategy executions',
    ['strategy_id', 'result']
)

strategy_execution_time = Histogram(
    'strategy_execution_seconds',
    'Strategy execution time',
    ['strategy_id']
)


class RealEngine:
    """
    Real production engine implementation

    Interfaces with real services:
    - ClickHouse for historical features
    - PostgreSQL for positions/portfolio
    - MarketDataFacade for poll-based feature streaming
    - Decision pipeline for order submission
    """

    def __init__(self, ch, strategy_engine=None, decision_pipeline=None, market_data_service=None):
        self.ch = ch  # ClickHouse client from global pool
        self._cadences: Dict[str, int] = {}  # strategy_id -> cadence_s
        self._strategy_engine = strategy_engine  # Back-reference to StrategyEngine
        self.decision_pipeline = decision_pipeline  # Decision pipeline service
        self.market_data_service = market_data_service  # Market data service for dynamic subscriptions

        # Import PostgreSQL for position queries
        from libs.db.postgres import PostgresDB
        self.pg = PostgresDB()
        self.order_truth_service: OrderTruthService | None = None
        self.broker_adapter = None

        logger.info("RealEngine initialized")

    async def emit_features(self, strategy_id: str, symbol: str, features: Dict[str, Any]) -> None:
        """
        Emit-driven execution hook for RFE.

        Called by StrategyContext when realtime feature engine (RFE) finalizes an emit_on bar.
        Delegates to StrategyEngine.execute_strategy.
        """
        try:
            if self._strategy_engine is None:
                logger.warning("emit_features_no_strategy_engine", strategy_id=strategy_id, symbol=symbol)
                return
            await self._strategy_engine.execute_strategy(strategy_id, symbol, features)
        except Exception as e:
            logger.error("emit_features_failed", strategy_id=strategy_id, symbol=symbol, error=str(e))

    async def subscribe_features(
        self,
        strategy_id: str,
        symbols: List[str],
        bar_sec: int
    ) -> None:
        """Subscribe to feature stream using poll-based approach"""
        # Delegate to StrategyEngine's unified subscription method
        if self._strategy_engine:
            await self._strategy_engine.subscribe_features_unified(
                strategy_id, symbols, bar_sec
            )
        else:
            logger.warning("StrategyEngine not set, cannot subscribe to features",
                          strategy_id=strategy_id,
                          symbols=symbols,
                          bar_sec=bar_sec)

    async def history_features(
        self,
        symbol: str,
        resolution: str,
        limit: int,
        fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Fetch historical features using unified approach"""
        # Delegate to StrategyEngine's unified history method
        if self._strategy_engine:
            return await self._strategy_engine.history_features_unified(
                symbol, resolution, limit
            )
        else:
            # Fallback to direct ClickHouse query
            table = f"features_{resolution}"

            sql = f"""
            SELECT *
            FROM {table} FINAL
            WHERE symbol = {{symbol:String}}
            ORDER BY ts DESC
            LIMIT {{limit:UInt32}}
            """

            results = await asyncio.to_thread(
                lambda: get_client().query(
                    sql,
                    parameters={
                        'symbol': symbol,
                        'limit': limit,
                    },
                )
            )

            # Reverse to get chronological order
            return list(reversed(results))

    async def portfolio_view(self, strategy_id: str) -> Dict[str, Any]:
        """Get portfolio view from PostgreSQL"""
        # Query strategy ledger
        ledger = await self.pg.fetchrow(
            "SELECT * FROM core.core_strategy_ledger WHERE strategy_id = $1",
            strategy_id
        )

        if not ledger:
            # Return default empty portfolio
            return {
                'cash': 0.0,
                'equity': 0.0,
                'positions': []
            }

        # Query positions
        positions = await self.pg.fetchall(
            "SELECT * FROM core.core_position WHERE strategy_id = $1 AND pos_qty != 0",
            strategy_id
        )

        return {
            'cash': float(ledger['reserved_usd']),
            'equity': float(ledger['reserved_usd'] + ledger['realized_pnl_usd']),
            'realized_pnl': float(ledger['realized_pnl_usd']),
            'positions': [dict(p) for p in positions]
        }

    async def position_view(self, strategy_id: str, symbol: str) -> PositionSnapshot:
        """Get effective position for strategy runtime gating."""
        truth = getattr(self, "order_truth_service", None) or RealEngine._build_order_truth_service(self)
        snapshot = await truth.effective_position(strategy_id=strategy_id, symbol=symbol)
        return snapshot.position

    async def open_orders_view(self, strategy_id: str) -> List[OrderUpdate]:
        """Get effective open orders for strategy runtime gating."""
        truth = getattr(self, "order_truth_service", None) or RealEngine._build_order_truth_service(self)
        snapshot = await truth.effective_open_orders(strategy_id=strategy_id)
        return list(snapshot.orders)

    def _build_order_truth_service(self) -> OrderTruthService:
        service = OrderTruthService(
            pg=self.pg,
            broker_open_order_source=getattr(self, "broker_adapter", None),
        )
        self.order_truth_service = service
        return service

    async def submit_intent(self, strategy_id: str, envelope: DecisionEnvelope) -> Any:
        """Submit trading intent to decision pipeline"""
        import os
        trading_mode = os.getenv('TRADING_MODE', 'paper')

        # Process through decision pipeline if available
        if self.decision_pipeline:
            try:
                from services.decision.pipeline import PipelineResult

                result: PipelineResult = await self.decision_pipeline.process_decision(envelope)

                # Log result to dry_run_orders table for tracking
                if trading_mode == 'dry_run':
                    # Determine side and qty based on result
                    if result.status == "APPROVED" and result.plan:
                        side = result.plan.side
                        qty = result.plan.qty
                        limit_px = result.plan.limit_px
                        reason = f"pipeline_approved:{result.plan.order_type}"
                    else:
                        side = 'BUY' if envelope.target_qty > 0 else 'SELL'
                        qty = abs(envelope.target_qty)
                        limit_px = None
                        reason = f"pipeline_{result.status.lower()}:{','.join(result.reasons)}"

                    await self.pg.execute(
                        """
                        INSERT INTO core.dry_run_orders (
                            dry_run_id, decision_id, strategy_id, symbol,
                            side, qty, order_type, limit_px, timestamp, would_submit, reason
                        ) VALUES (
                            gen_random_uuid()::text, $1, $2, $3, $4, $5, $6, $7,
                            now(), $8, $9
                        )
                        """,
                        envelope.decision_id,
                        strategy_id,
                        envelope.symbol,
                        side,
                        qty,
                        result.plan.order_type if result.status == "APPROVED" and result.plan else "UNKNOWN",
                        limit_px,
                        result.status == "APPROVED",  # would_submit
                        reason
                    )

                # Execution is handled inside decision pipeline; keep this as debug trace only.
                if trading_mode != 'dry_run' and result.status == "APPROVED":
                    logger.debug(
                        "Active mode decision approved",
                        strategy_id=strategy_id,
                        decision_id=envelope.decision_id,
                        symbol=envelope.symbol,
                    )
                return result

            except Exception as e:
                logger.error("Decision pipeline processing failed",
                           strategy_id=strategy_id,
                           decision_id=envelope.decision_id,
                           error=str(e))
                return {"status": "ERROR", "reasons": [str(e)]}
        else:
            # Fallback: Legacy dry_run logging
            if trading_mode == 'dry_run':
                await self.pg.execute(
                    """
                    INSERT INTO core.dry_run_orders (
                        dry_run_id, decision_id, strategy_id, symbol,
                        side, qty, order_type, limit_px, timestamp, would_submit, reason
                    ) VALUES (
                        gen_random_uuid()::text, $1, $2, $3, $4, $5, $6, $7,
                        now(), true, 'strategy_decision_no_pipeline'
                    )
                    """,
                    envelope.decision_id,
                    strategy_id,
                    envelope.symbol,
                    'BUY' if envelope.target_qty > 0 else 'SELL',
                    abs(envelope.target_qty),
                    envelope.order_style.kind if envelope.order_style else 'MKT',
                    envelope.order_style.limit_px if envelope.order_style else None
                )

                logger.info("Intent logged to dry_run_orders (no pipeline)",
                           strategy_id=strategy_id,
                           decision_id=envelope.decision_id,
                           symbol=envelope.symbol,
                           target_qty=envelope.target_qty)
                return {"status": "APPROVED", "reasons": ["dry_run_logged_no_pipeline"]}
            else:
                logger.warning("Decision pipeline not configured",
                              strategy_id=strategy_id,
                              decision_id=envelope.decision_id,
                              symbol=envelope.symbol)
                return {"status": "UNKNOWN", "reasons": ["decision_pipeline_not_configured"]}

    async def set_cadence(self, strategy_id: str, seconds: int) -> None:
        """Set strategy cadence"""
        self._cadences[strategy_id] = max(5, seconds)  # Minimum 5s
        logger.info("Cadence set",
                   strategy_id=strategy_id,
                   cadence_s=seconds)

    async def subscribe_tick_data(self, strategy_id: str, symbols: List[str]) -> Dict[str, bool]:
        """
        Subscribe to raw tick data (triggers IB subscription + Kafka publishing).

        Args:
            strategy_id: Strategy ID requesting subscription
            symbols: List of stock symbols to subscribe

        Returns:
            Dict[symbol, success]: Subscription result for each symbol

        Note:
            - This is different from subscribe_features() which only consumes Kafka
            - This triggers actual IB reqMktData() calls
            - Data will start flowing after 2-5 seconds (IBKR delay)
        """
        if not self.market_data_service:
            logger.error("MarketDataService not available, cannot subscribe to tick data",
                        strategy_id=strategy_id,
                        symbols=symbols)
            return {symbol: False for symbol in symbols}

        results = {}
        for symbol in symbols:
            try:
                success = await self.market_data_service.subscribe_symbol(symbol)
                results[symbol] = success

                if success:
                    logger.info(
                        "Strategy tick subscription accepted",
                        strategy_id=strategy_id,
                        symbol=symbol,
                    )
                else:
                    logger.warning(
                        "Strategy tick subscription not accepted",
                        strategy_id=strategy_id,
                        symbol=symbol,
                    )
            except Exception as e:
                logger.error("Error subscribing strategy to tick data",
                            strategy_id=strategy_id,
                            symbol=symbol,
                            error=str(e))
                results[symbol] = False

        return results


class StrategyEngine:
    """
    Strategy execution engine

    Manages:
    - Strategy lifecycle (initialize, run, shutdown)
    - Independent execution cadences
    - Event distribution
    - Error handling and recovery
    """

    def __init__(
        self,
        market_data_facade=None,
        decision_pipeline=None,
        price_cache=None,
        market_data_service=None,
        system_health_controller: Optional[SystemHealthController] = None,
        dry_run: bool = False,
    ):
        """Initialize strategy engine

        Args:
            market_data_facade: Market data facade (optional)
            decision_pipeline: Decision pipeline service (optional)
            price_cache: PriceCache instance for low-latency L1 data (optional)
            market_data_service: MarketDataService for dynamic tick subscriptions (optional)
        """
        self.registry = get_registry()
        self.dry_run = bool(dry_run)
        if self.dry_run:
            self.ch = None
        else:
            self.ch = get_client()  # Use global connection pool

        # Market data facade for strategy integration
        self.md = market_data_facade
        if self.md is None:
            if self.dry_run:
                from services.strategy.dry_run import NoopMarketDataFacade

                self.md = NoopMarketDataFacade()
            else:
                # Create default facade
                from libs.dal.market_data_facade import MarketDataFacade
                self.md = MarketDataFacade(ch=self.ch)

        # Decision pipeline
        self.decision_pipeline = decision_pipeline

        # PriceCache for low-latency L1 data
        self.price_cache = price_cache

        # Market data service for dynamic subscriptions
        self.market_data_service = market_data_service
        if system_health_controller is not None:
            self.system_health_controller = system_health_controller
        elif self.dry_run:
            from services.strategy.dry_run import NoopHealthController

            self.system_health_controller = NoopHealthController()
        else:
            self.system_health_controller = SystemHealthController()

        # Create RealEngine with back-reference for feature subscription
        if self.dry_run:
            from services.strategy.dry_run import DryRunEngine

            self.engine = DryRunEngine()
        else:
            self.engine = RealEngine(
                self.ch,
                strategy_engine=self,
                decision_pipeline=self.decision_pipeline,
                market_data_service=self.market_data_service
            )

        # Strategy instances
        self._strategies: Dict[str, StrategyAlgorithm] = {}  # strategy_id -> instance
        self._cadences: Dict[str, int] = {}  # strategy_id -> cadence_s
        self._last_exec: Dict[str, float] = {}  # strategy_id -> last_exec_time
        self._inference: Dict[str, InferenceManager] = {}
        # Specs cache for potential reloads
        self._specs: Dict[str, Dict[str, Any]] = {}
        # Slow-exec warning state (observability only; does not affect execution path)
        self._slow_exec_consecutive: Dict[str, int] = {}
        self._slow_exec_last_warn_ts: Dict[str, float] = {}
        self._slow_exec_warn_cooldown_s: float = 60.0
        self._slow_exec_warn_consecutive: int = 3

        # Polling tasks for real-time data
        self._poll_tasks: List[asyncio.Task] = []
        self._poll_stream_tasks: Dict[tuple[str, str], asyncio.Task] = {}
        self._poll_stream_consumers: Dict[tuple[str, str], set[str]] = {}
        self._poll_stream_consumer_symbols: Dict[tuple[str, str], Dict[str, str]] = {}
        self._bar_callback_minute_seen: Dict[tuple[str, str, str], str] = {}
        self._bar_callback_info_last_emit: Dict[tuple[str, str], datetime] = {}
        try:
            self._bar_callback_info_every_min = max(
                0, int(os.getenv("STRATEGY_BAR_HEARTBEAT_INFO_EVERY_MIN", "5"))
            )
        except Exception:
            self._bar_callback_info_every_min = 5

        # Control
        self._running = False

        if decision_pipeline:
            is_dry_run = bool(getattr(decision_pipeline, "dry_run_mode", False))
            pipeline_status = "ENABLED (dry_run)" if is_dry_run else "ENABLED (active)"
        else:
            pipeline_status = "DISABLED"
        logger.info(f"StrategyEngine initialized (pipeline={pipeline_status}, dry_run={self.dry_run})")

    def _subscribe_price_cache_symbols(self, symbols: List[str]) -> None:
        subscribe = getattr(self.price_cache, "subscribe", None)
        if not callable(subscribe):
            return
        normalized = []
        for symbol in list(symbols or []):
            if symbol is None:
                continue
            symbol_u = str(symbol).strip().upper()
            if not symbol_u or symbol_u == "NONE":
                continue
            normalized.append(symbol_u)
        if not normalized:
            return
        try:
            subscribe(normalized)
        except Exception as exc:
            logger.warning("price_cache_subscribe_failed", symbols=normalized, error=str(exc))

    def _should_emit_bar_heartbeat_info(
        self,
        *,
        strategy_id: str,
        resolution: str,
        minute_key: str,
    ) -> bool:
        """
        Throttle noisy callback heartbeat logs.
        Keep audit events at per-symbol granularity, but only emit terminal info
        logs every N minutes per strategy+resolution.
        """
        if self._bar_callback_info_every_min <= 0:
            return False
        try:
            minute_dt = datetime.strptime(minute_key, "%Y-%m-%d %H:%M")
        except Exception:
            return False
        key = (str(strategy_id), str(resolution))
        last = self._bar_callback_info_last_emit.get(key)
        if last is None or (minute_dt - last).total_seconds() >= float(self._bar_callback_info_every_min * 60):
            self._bar_callback_info_last_emit[key] = minute_dt
            return True
        return False

    async def load_strategy(
        self,
        strategy_id: str,
        name: str,
        params: Dict[str, Any]
    ) -> None:
        """
        Load and initialize a strategy

        Args:
            strategy_id: Unique strategy identifier
            name: Strategy name (registered)
            params: Strategy parameters
        """
        # Create context
        if self.dry_run:
            from services.strategy.dry_run import DryRunStrategyContext

            ctx = DryRunStrategyContext(
                engine=self.engine,
                strategy_id=strategy_id,
                logger=logger,
                metrics=None,
                price_cache=self.price_cache,
            )
        else:
            ctx = StrategyContext(
                engine=self.engine,
                strategy_id=strategy_id,
                logger=logger,
                metrics=None,  # TODO: Add metrics
                price_cache=self.price_cache
            )
        self.system_health_controller.attach_context(ctx)
        try:
            from strategies.stocks_kernel.assembly import legacy_kernel_assembly_provider
        except ModuleNotFoundError:
            if not self.dry_run:
                raise
            legacy_kernel_assembly_provider = None
        setattr(ctx, "kernel_assembly_provider", legacy_kernel_assembly_provider)

        # Load strategy instance
        strategy = self.registry.load(name, params, ctx)
        setattr(ctx, "params", strategy.params)
        await self.system_health_controller.register_strategy(strategy_id, strategy)

        # Initialize
        await strategy.initialize()
        await self.system_health_controller.assert_strategy_admitted(strategy_id)

        extras = params.get("extras") or {}
        inf_cfg = extras.get("inference")
        if inf_cfg and not self.dry_run:
            try:
                self._inference[strategy_id] = InferenceManager(inf_cfg)
                logger.info("InferenceManager attached", strategy_id=strategy_id)
            except Exception as exc:
                logger.warning("Failed to initialize inference manager", strategy_id=strategy_id, error=str(exc))

        # Store
        self._strategies[strategy_id] = strategy
        self._cadences[strategy_id] = strategy.params.cadence_s
        self._last_exec[strategy_id] = 0
        self._specs[strategy_id] = {"name": name, "params": params}

        logger.info("Strategy loaded",
                   strategy_id=strategy_id,
                   name=name,
                   cadence_s=strategy.params.cadence_s)

    async def unload_strategy(self, strategy_id: str, reason: str = "manual") -> None:
        """
        Unload a strategy

        Args:
            strategy_id: Strategy identifier
            reason: Shutdown reason
        """
        if strategy_id not in self._strategies:
            return

        strategy = self._strategies[strategy_id]

        # Shutdown
        await strategy.on_shutdown(reason)

        # Remove
        del self._strategies[strategy_id]
        del self._cadences[strategy_id]
        del self._last_exec[strategy_id]
        self._inference.pop(strategy_id, None)
        self._specs.pop(strategy_id, None)
        self._slow_exec_consecutive.pop(strategy_id, None)
        self._slow_exec_last_warn_ts.pop(strategy_id, None)

        # Remove strategy from shared stream consumers; cancel stream when no consumers remain.
        for stream_key in list(self._poll_stream_consumers.keys()):
            consumers = self._poll_stream_consumers.get(stream_key)
            if not consumers:
                continue
            if strategy_id not in consumers:
                continue
            consumers.discard(strategy_id)
            symbol_map = self._poll_stream_consumer_symbols.get(stream_key) or {}
            symbol_map.pop(strategy_id, None)
            if symbol_map:
                self._poll_stream_consumer_symbols[stream_key] = symbol_map
            else:
                self._poll_stream_consumer_symbols.pop(stream_key, None)
            if consumers:
                self._poll_stream_consumers[stream_key] = consumers
                continue
            self._poll_stream_consumers.pop(stream_key, None)
            task = self._poll_stream_tasks.pop(stream_key, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                if task in self._poll_tasks:
                    self._poll_tasks.remove(task)

        logger.info("Strategy unloaded",
                   strategy_id=strategy_id,
                   reason=reason)

    async def reload_strategy(self, strategy_id: str) -> None:
        """Hot-reload a strategy by re-reading its spec from config.yml and reloading."""
        import yaml
        from copy import deepcopy
        try:
            with open('config.yml', 'r') as f:
                cfg = yaml.safe_load(f)
        except Exception as exc:
            logger.error("Failed to read config.yml for reload", error=str(exc))
            return

        strategies_cfg = (cfg or {}).get('strategies') or []
        registry_cfg = (cfg or {}).get('strategy_registry') or {}
        spec = None
        for s in strategies_cfg:
            if s.get('id') == strategy_id:
                spec = s
                break
        if not spec:
            logger.warning("Reload spec not found", strategy_id=strategy_id)
            return

        module = spec.get('module')
        cls_name = spec.get('class')
        if not module or not cls_name:
            logger.warning("Malformed spec for reload", strategy_id=strategy_id, spec=spec)
            return

        # Determine registry name
        module_path = f"{module}.{cls_name}"
        name = None
        for reg_name, path in (registry_cfg or {}).items():
            if path == module_path:
                name = reg_name
                break
        if name is None:
            try:
                mod = __import__(module, fromlist=[cls_name])
                cls = getattr(mod, cls_name)
                name = spec.get('registry_name', strategy_id)
                self.registry.register(name, cls)
            except Exception as exc:
                logger.error("Failed to prepare class for reload", error=str(exc), module=module_path)
                return

        params = deepcopy(spec.get('params') or {})

        # Unload if present then load
        if strategy_id in self._strategies:
            await self.unload_strategy(strategy_id, reason="hot_reload")
        await self.load_strategy(strategy_id=strategy_id, name=name, params=params)

    async def execute_strategy(
        self,
        strategy_id: str,
        symbol: str,
        features: Dict[str, Any]
    ) -> None:
        """
        Execute strategy on feature update

        Args:
            strategy_id: Strategy identifier
            symbol: Symbol
            features: Feature dict
        """
        if strategy_id not in self._strategies:
            return

        strategy = self._strategies[strategy_id]
        features = dict(features)

        inf_mgr = self._inference.get(strategy_id)
        if inf_mgr:
            enriched, diag = inf_mgr.infer_row(dict(features))
            features = enriched
            features.setdefault("ml_infer_ms", diag.get("infer_ms"))
            features.setdefault("ml_infer_error", diag.get("error"))

        start_time = time.time()

        try:
            # Execute strategy logic
            await strategy.on_features(symbol, features)

            # Record metrics
            elapsed = time.time() - start_time
            elapsed_ms = elapsed * 1000.0
            strategy_execution_time.labels(strategy_id=strategy_id).observe(elapsed)
            strategy_executions.labels(strategy_id=strategy_id, result='success').inc()

            # Slow-exec warning is cadence-aware and debounced to avoid noisy alerts.
            cadence_s = int(self._cadences.get(strategy_id, getattr(strategy.params, "cadence_s", 5)) or 5)
            cadence_s = max(cadence_s, 1)
            slow_warn_threshold_ms = max(200.0, cadence_s * 1000.0 * 0.5)

            if elapsed_ms > slow_warn_threshold_ms:
                n = self._slow_exec_consecutive.get(strategy_id, 0) + 1
                self._slow_exec_consecutive[strategy_id] = n

                now_ts = time.time()
                last_warn_ts = self._slow_exec_last_warn_ts.get(strategy_id, 0.0)
                should_warn = (
                    n >= self._slow_exec_warn_consecutive
                    and (now_ts - last_warn_ts) >= self._slow_exec_warn_cooldown_s
                )
                if should_warn:
                    self._slow_exec_last_warn_ts[strategy_id] = now_ts
                    logger.warning(
                        "Strategy execution slow",
                        strategy_id=strategy_id,
                        elapsed_ms=round(elapsed_ms, 2),
                        cadence_s=cadence_s,
                        warn_threshold_ms=round(slow_warn_threshold_ms, 2),
                        consecutive=n,
                    )
            else:
                self._slow_exec_consecutive[strategy_id] = 0

        except Exception as e:
            # Handle error
            logger.error("Strategy execution failed",
                        strategy_id=strategy_id,
                        error=str(e))

            strategy_executions.labels(strategy_id=strategy_id, result='error').inc()

            # Call error handler
            try:
                await strategy.on_error(e, where="on_features")
            except Exception as e2:
                logger.error("Error handler failed",
                            strategy_id=strategy_id,
                            error=str(e2))

    async def run_backtest(
        self,
        strategy_id: str,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> Dict[str, Any]:
        """
        Run strategy backtest

        Args:
            strategy_id: Strategy identifier
            symbol: Symbol
            start_time: Backtest start
            end_time: Backtest end

        Returns:
            Backtest results
        """
        if strategy_id not in self._strategies:
            raise ValueError(f"Strategy not loaded: {strategy_id}")

        strategy = self._strategies[strategy_id]

        logger.info("Starting backtest",
                   strategy_id=strategy_id,
                   symbol=symbol,
                   start=start_time,
                   end=end_time)

        # Fetch historical features
        sql = """
        SELECT *
        FROM features_5s FINAL
        WHERE symbol = {symbol:String}
          AND ts >= {start:DateTime}
          AND ts < {end:DateTime}
        ORDER BY ts ASC
        """

        features = await asyncio.to_thread(
            lambda: get_client().query(
                sql,
                parameters={
                    'symbol': symbol,
                    'start': start_time,
                    'end': end_time,
                },
            )
        )

        logger.info("Backtest data loaded",
                   bars=len(features))

        # Execute strategy on each bar
        for i, feat in enumerate(features):
            await self.execute_strategy(strategy_id, symbol, feat)

            if (i + 1) % 100 == 0:
                logger.info("Backtest progress",
                           bars_processed=i + 1,
                           total=len(features))

        logger.info("Backtest complete",
                   strategy_id=strategy_id,
                   bars_processed=len(features))

        return {
            'strategy_id': strategy_id,
            'symbol': symbol,
            'bars_processed': len(features),
            'start': start_time,
            'end': end_time
        }

    async def shutdown(self) -> None:
        """Shutdown all strategies"""
        logger.info("Shutting down strategy engine...")

        # Cancel polling tasks
        for task in self._poll_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._poll_stream_tasks.clear()
        self._poll_stream_consumers.clear()
        self._poll_stream_consumer_symbols.clear()

        for strategy_id in list(self._strategies.keys()):
            await self.unload_strategy(strategy_id, reason="engine_shutdown")

        # Note: ClickHouse client uses global connection pool - no close needed

        logger.info("Strategy engine shutdown complete")

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
        sym = await self.md.vix_symbol_async() if symbol == "VIX" else symbol

        try:
            return await self.md.bars_async(sym, res, limit)
        except Exception as e:
            logger.error("Failed to fetch history", error=str(e), symbol=symbol, res=res)
            return []

    async def subscribe_features_unified(
        self,
        strategy_id: str,
        symbols: List[str],
        bar_sec: int,
        quote_staleness_sec: int = 5,
        poll_sec_1m: int = 3,
        poll_sec_5m: int = 10
    ) -> None:
        """
        Subscribe to features using poll-based approach.
        Polls ClickHouse for new bars and triggers strategy callbacks.

        Args:
            strategy_id: Strategy ID
            symbols: List of symbols to subscribe
            bar_sec: Bar resolution in seconds (60 for 1m, 300 for 5m)
            quote_staleness_sec: Max age of L1 quotes
            poll_sec_1m: Polling interval for 1m bars
            poll_sec_5m: Polling interval for 5m bars
        """
        self._subscribe_price_cache_symbols(symbols)

        # If strategy is configured for RFE, skip poll-based subscription to prevent double-calls
        try:
            specs = getattr(self._strategy_engine, "_specs", {}) or {}
            spec = specs.get(strategy_id, {})
            extras = (spec.get("params") or {}).get("extras") or {}
            rfe_cfg = (extras.get("realtime_feature_engine") or {})
            if bool(rfe_cfg.get("enabled", False)):
                logger.info(
                    "skip_poll_due_to_rfe",
                    strategy_id=strategy_id,
                    symbols=symbols,
                    bar_sec=bar_sec,
                    emit_on=rfe_cfg.get("emit_on", "1m")
                )
                return
        except Exception:
            pass

        if not self.md:
            logger.warning("MarketDataFacade not configured, cannot subscribe")
            return

        # Determine resolution and poll interval
        if bar_sec <= 5:
            res = "5s"
            poll_sec = 1
        elif bar_sec <= 60:
            res = "1m"
            poll_sec = poll_sec_1m
        else:
            res = "5m"
            poll_sec = poll_sec_5m

        logger.info(f"Subscribing {strategy_id} to {symbols} @ {res} (poll every {poll_sec}s)")

        # Create polling task for each symbol
        for symbol in symbols:
            # Handle VIX aliasing
            actual_symbol = await self.md.vix_symbol_async() if symbol == "VIX" else symbol
            stream_key = (str(actual_symbol), str(res))
            existing = self._poll_stream_tasks.get(stream_key)
            if existing is not None and not existing.done():
                consumers = self._poll_stream_consumers.setdefault(stream_key, set())
                consumers.add(str(strategy_id))
                symbol_map = self._poll_stream_consumer_symbols.setdefault(stream_key, {})
                symbol_map[str(strategy_id)] = str(symbol)
                logger.info(
                    "reuse_existing_bar_stream",
                    strategy_id=str(strategy_id),
                    symbol=str(symbol),
                    actual_symbol=str(actual_symbol),
                    resolution=str(res),
                    consumer_count=len(consumers),
                )
                continue

            # Create callback for this symbol
            async def on_new_bar(bar_row: Dict[str, Any], sym=actual_symbol, stream_key_local=stream_key):
                """Callback when new bar is detected"""
                try:
                    # Build feature dict from bar
                    base_features = dict(bar_row)
                    ts_obj = base_features.get("ts")
                    minute_key = ""
                    try:
                        if isinstance(ts_obj, datetime):
                            minute_key = ts_obj.strftime("%Y-%m-%d %H:%M")
                        elif ts_obj is not None:
                            minute_key = str(ts_obj)[:16]
                    except Exception:
                        minute_key = ""

                    consumers = list(self._poll_stream_consumers.get(stream_key_local, set()))
                    symbol_map = dict(self._poll_stream_consumer_symbols.get(stream_key_local) or {})
                    for sid in consumers:
                        orig_sym = str(symbol_map.get(str(sid)) or "")
                        if not orig_sym:
                            continue
                        features = dict(base_features)

                        # For trading symbols (not SPY/QQQ/VIX/VIXY), add L1 quotes
                        if orig_sym not in ("SPY", "QQQ", "VIX", "VIXY"):
                            quote = await self.md.last_quote_async(sym, staleness_sec=quote_staleness_sec)
                            features.update(quote)

                        # Lightweight observability: one callback heartbeat per minute per strategy/symbol/resolution.
                        # This proves the feature callback is alive, but it is not a strategy tradability signal.
                        try:
                            hb_key = (str(sid), str(orig_sym), str(res))
                            if minute_key and self._bar_callback_minute_seen.get(hb_key) != minute_key:
                                self._bar_callback_minute_seen[hb_key] = minute_key
                                if self._should_emit_bar_heartbeat_info(
                                    strategy_id=str(sid),
                                    resolution=str(res),
                                    minute_key=str(minute_key),
                                ):
                                    logger.info(
                                        "strategy_bar_callback_heartbeat",
                                        strategy_id=str(sid),
                                        symbol=str(orig_sym),
                                        resolution=str(res),
                                        minute=minute_key,
                                        sampled=True,
                                        sampled_every_min=self._bar_callback_info_every_min,
                                    )
                                audit_writer = getattr(self, "audit_writer", None)
                                if audit_writer is not None and hasattr(audit_writer, "emit"):
                                    try:
                                        audit_writer.emit(
                                            domain="strategy",
                                            component="strategy_engine",
                                            stage="feature_callback_heartbeat",
                                            severity="info",
                                            strategy_id=str(sid),
                                            symbol=str(orig_sym),
                                            status="alive",
                                            reason="new_bar_callback",
                                            payload={
                                                "resolution": str(res),
                                                "minute": str(minute_key),
                                                "bar_ts": str(features.get("ts") or ""),
                                            },
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                        # Get strategy instance and call execute_strategy
                        if sid in self._strategies:
                            if sid == "RevF-001":
                                try:
                                    inst = self._strategies[sid]
                                    cls_name = type(inst).__name__
                                    mod_name = inst.__class__.__module__
                                    logger.info(
                                        "BAR_INGEST_5S",
                                        strategy=sid,
                                        symbol=orig_sym,
                                        res=res,
                                        ts=str(features.get("ts") or features.get("ts_start") or features.get("ts_utc")),
                                        module=mod_name,
                                        cls=cls_name
                                    )
                                    logger.info(
                                        "CALLING_STRATEGY",
                                        strategy=sid,
                                        symbol=orig_sym,
                                        module=mod_name,
                                        cls=cls_name,
                                        method="on_features"
                                    )
                                except Exception:
                                    pass
                            await self.execute_strategy(sid, orig_sym, features)

                except Exception as e:
                    logger.error("Error in bar callback", error=str(e), symbol=str(symbol), strategy=str(strategy_id))

            # Start polling task
            task = asyncio.create_task(
                self.md.poll_new_bar(actual_symbol, res, poll_sec, on_new_bar)
            )
            self._poll_tasks.append(task)
            self._poll_stream_tasks[stream_key] = task
            self._poll_stream_consumers[stream_key] = {str(strategy_id)}
            self._poll_stream_consumer_symbols[stream_key] = {str(strategy_id): str(symbol)}

        logger.info(f"✅ Subscribed {strategy_id} to {len(symbols)} symbols")
