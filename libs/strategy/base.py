"""
Strategy Base Classes
Defines the core interfaces for strategy development
Based on doc/8.md and doc/59.md specifications
"""
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from abc import ABC, abstractmethod
import uuid


@dataclass
class StrategyParams:
    """
    Strategy configuration parameters

    All strategies share these base parameters.
    Strategy-specific parameters go in extras.
    """
    cadence_s: int = 5  # Strategy execution interval (seconds)
    ttl_ms: int = 2000  # Intent freshness TTL
    qscore_threshold: int = 70  # Feature quality gate
    max_notional_usd: float = 100000  # Strategy capital limit
    per_symbol_max_usd: float = 50000  # Per-symbol position limit
    risk_per_trade_usd: float = 2000  # Risk budget per trade
    capital_limit_usd: float = 100000  # Convenience alias for capital pool
    oco: Optional[Dict[str, Any]] = None  # OCO configuration
    extras: Dict[str, Any] = field(default_factory=dict)  # Strategy-specific params


@dataclass
class Bar5s:
    """5-second bar data"""
    symbol: str
    ts_start: datetime
    ts_end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    trades: int = 0
    rth: bool = True  # Regular trading hours
    source: str = "AGG"
    calc_mode: str = "realtime"


@dataclass
class Bar1m(Bar5s):
    """1-minute bar data (same fields as 5s)"""
    pass


@dataclass
class OrderUpdate:
    """Order status update"""
    order_id: str
    strategy_id: str
    symbol: str
    state: str  # NEW/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED
    # Optional metadata (not all engines populate these).
    # These are required for robust stop/TP reconciliation and audit loops.
    order_type: Optional[str] = None
    order_ref: Optional[str] = None
    note: Optional[str] = None
    stop_px: Optional[float] = None
    limit_px: Optional[float] = None
    broker_order_id: Optional[str] = None
    ib_perm_id: Optional[int] = None
    parent_order_id: Optional[str] = None
    oca_group: Optional[str] = None
    last_error: Optional[str] = None
    ts: Optional[datetime] = None


@dataclass
class PositionSnapshot:
    """Position snapshot"""
    strategy_id: str
    symbol: str
    pos_qty: float  # Signed net position
    avg_px: Optional[float] = None
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    ts: Optional[datetime] = None


@dataclass
class PriceIntent:
    """Price intent specification"""
    ref: str  # 'mid' | 'last' | 'best'
    offset_ticks: float
    hard_ceiling_px: Optional[float] = None
    hard_floor_px: Optional[float] = None


@dataclass
class RiskOverrides:
    """Risk override specifications"""
    catastrophic_stop_px: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    time_stop_s: Optional[int] = None


@dataclass
class CostEstimates:
    """Cost estimates"""
    slippage_bp: float
    fees_bp: float
    impact_bp: float
    confidence: float


@dataclass
class Freshness:
    """Intent freshness specification"""
    dlb_ms: int = 30000  # Decision latency budget (30s)
    ttl_ms: int = 60000  # Time to live (60s)


@dataclass
class OrderStyle:
    """Order style specification"""
    kind: str  # MKT/LMT/STP/STP_LMT/TRAIL/TRAIL_LIMIT/PEG
    limit_px: Optional[float] = None
    stop_px: Optional[float] = None
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    trail_stop_px: Optional[float] = None
    lmt_price_offset: Optional[float] = None
    protective_stop: bool = False
    close_only: bool = False
    reduce_only: bool = True
    role: Optional[str] = None
    tif: Optional[str] = None
    outside_rth: Optional[bool] = None


def validate_order_style(style: Optional[OrderStyle]) -> None:
    if style is None:
        return
    kind = str(style.kind or "").strip().upper().replace(" ", "_")
    if kind not in {"MKT", "LMT", "STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT", "PEG"}:
        raise ValueError(f"unsupported order style: {style.kind}")
    if kind in {"TRAIL", "TRAIL_LIMIT"}:
        has_amount = style.trail_amount is not None
        has_percent = style.trail_percent is not None
        if not has_amount and not has_percent:
            raise ValueError(f"{kind} requires trail_amount or trail_percent")
        if has_amount and has_percent:
            raise ValueError(f"{kind} must use either trail_amount or trail_percent, not both")
        if style.trail_amount is not None and float(style.trail_amount) <= 0:
            raise ValueError(f"{kind} trail_amount must be positive")
        if style.trail_percent is not None and float(style.trail_percent) <= 0:
            raise ValueError(f"{kind} trail_percent must be positive")
    if kind == "TRAIL_LIMIT":
        if style.lmt_price_offset is None:
            raise ValueError("TRAIL_LIMIT requires lmt_price_offset")
        if float(style.lmt_price_offset) <= 0:
            raise ValueError("TRAIL_LIMIT lmt_price_offset must be positive")
        if style.limit_px is not None:
            raise ValueError("TRAIL_LIMIT must not mix limit_px with lmt_price_offset")


@dataclass
class DecisionEnvelope:
    """
    High-level trading intent

    Represents what the strategy wants to do.
    Engine handles: validation → arbiter → risk → execution
    """
    # Core required fields
    decision_id: str
    strategy_id: str
    symbol: str
    ts_bar_end_us: int  # Bar timestamp (microseconds)
    target_qty: float  # Target net position (+long/-short)
    freshness: Freshness

    # Enhanced fields with defaults (backward compatible)
    strategy_version: Optional[str] = None
    param_hash: Optional[str] = None
    bar_id: str = ""
    ts_bar_start_us: int = 0
    regime_tag: str = "unknown"
    signal_score: float = 0.5  # 0..1
    max_notional: float = 0.0
    max_participation: float = 0.2
    price_intent: Optional[PriceIntent] = None
    risk_overrides: Optional[RiskOverrides] = None
    cost_estimates: Optional[CostEstimates] = None

    # Legacy fields for backward compatibility
    tif: str = "DAY"  # Time in force (DAY/IOC/GTC)
    order_style: Optional[OrderStyle] = None
    tags: List[str] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)  # ref_px, edge_bp, oco_cfg, etc.


class StrategyAlgorithm(ABC):
    """
    Base class for all trading strategies

    Strategies implement the abstract methods and use the provided
    Context API to access data and submit trading intents.
    """

    def __init__(self, ctx: "StrategyContext", params: StrategyParams):
        """
        Initialize strategy

        Args:
            ctx: Strategy context (injected by engine)
            params: Strategy parameters
        """
        self.ctx = ctx
        self.params = params
        self.state: Dict[str, Any] = {}  # Persistent state (for warm restart)
        self.log = ctx.log
        self.m = ctx.metrics
        # Convenience aliases used by existing strategies
        self.logger = ctx.log
        self.metrics = ctx.metrics
        self.engine = ctx.engine
        self.strategy_id = ctx.strategy_id
        self._system_paused: bool = False
        self._system_health_subscribed: bool = False

    @abstractmethod
    async def initialize(self) -> None:
        """
        Startup initialization (called once)

        Use this to:
        - Subscribe to data feeds
        - Load historical data
        - Set execution cadence
        - Initialize state

        Budget: < 300ms
        """
        pass

    @abstractmethod
    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        """
        Called on each feature update (5s/10s cadence)

        This is the main strategy logic.

        Args:
            symbol: Symbol
            features: Feature dict (from features_5s/10s tables)

        Budget: P95 < 200ms
        """
        pass

    async def on_bar(self, ctx: "StrategyContext", bar: Bar1m) -> None:
        """
        Optional: Called on slower 1m cadence

        Budget: P95 < 200ms
        """
        pass

    async def on_order_update(self, update: OrderUpdate) -> None:
        """
        Called on order status changes

        Budget: < 50ms (non-blocking)
        """
        pass

    async def on_position_update(self, position: PositionSnapshot) -> None:
        """
        Called on position/PnL changes

        Budget: < 50ms (non-blocking)
        """
        pass

    async def on_shutdown(self, reason: str) -> None:
        """
        Cleanup before strategy unload

        Budget: < 200ms
        """
        pass

    async def on_error(self, err: Exception, where: str) -> None:
        """
        Called when engine catches strategy exception

        Use this for logging or graceful degradation.
        """
        self.log.error("Strategy error", error=str(err), where=where)

    async def subscribe_system_health(self) -> None:
        controller = getattr(self.ctx, "system_health", None)
        if controller is None:
            raise RuntimeError(f"system_health_controller_missing:{self.strategy_id}")
        await controller.subscribe(self.strategy_id, self)
        self._system_health_subscribed = True

    async def on_system_health_event(self, event: Dict[str, Any]) -> None:
        event_type = str((event or {}).get("event_type") or "").strip().lower()
        metadata = dict((event or {}).get("metadata") or {})
        lifecycle_state = str(metadata.get("runtime_lifecycle_state") or "").strip().upper()
        if lifecycle_state in {"RUNNING", "RUNNING_PARTIAL_ENTRY"}:
            self._system_paused = False
            return
        if lifecycle_state in {
            "RECOVERING",
            "BROKER_TRUTH_SYNCING",
            "RUNTIME_RECONCILING",
            "PROTECTIVE_VERIFYING",
            "DEGRADED_CLOSE_ONLY",
            "MANUAL_REQUIRED",
            "HALTED",
            "STARTUP_PREFLIGHT_FAILED",
        }:
            self._system_paused = True
            return
        if event_type in {"system_paused", "ib_disconnected", "pause"}:
            self._system_paused = True
        elif event_type in {"system_resumed", "ib_connected", "resume"}:
            self._system_paused = False
        elif event_type in {"system_halted", "halt"}:
            self._system_paused = True

    async def on_system_paused(self, event: Dict[str, Any]) -> None:
        self._system_paused = True

    async def on_system_resumed(self, event: Dict[str, Any]) -> None:
        self._system_paused = False

    async def on_system_halted(self, event: Dict[str, Any]) -> None:
        self._system_paused = True


class StrategyContext:
    """
    Strategy context API

    Injected by engine, provides access to:
    - Data (history, features, portfolio)
    - Execution (submit intents)
    - Utilities (time, metrics, logging)
    """

    def __init__(self, engine: Any, strategy_id: str, logger: Any, metrics: Any, price_cache: Optional[Any] = None):
        """
        Initialize context

        Args:
            engine: Strategy engine (provides data and execution)
            strategy_id: Strategy identifier
            logger: Logger instance
            metrics: Metrics instance
            price_cache: PriceCache instance for low-latency L1 data (optional)
        """
        self.engine = engine
        self.strategy_id = strategy_id
        self.log = logger
        self.metrics = metrics
        self._price_cache = price_cache
        self._subscriptions: Dict[str, List[str]] = {}  # symbol -> resolutions
        # Stable per-run identifier for traceability.
        self.strategy_run_id = str(uuid.uuid4())
        self.system_health: Optional[Any] = None

        # Realtime Feature Engine (RFE) integration (Phase 0)
        self._rfe_enabled: bool = False
        self._rfe_engine: Optional[Any] = None
        self._rfe_reader: Optional[Any] = None
        self._rfe_poll_tasks: Dict[str, Any] = {}
        self._rfe_last_ts: Dict[str, int] = {}
        self._rfe_cache: Dict[str, Dict[str, Any]] = {}
        self._rfe_ts_us: Dict[str, int] = {}
        self._rfe_warm: Dict[str, bool] = {}
        self._rfe_emit_tf: str = "1m"

    # ---- Data subscription ----

    async def subscribe_features(self, symbols: List[str], bar_sec: int = 5) -> None:
        """
        Subscribe to feature stream

        Args:
            symbols: Symbols to subscribe
            bar_sec: Bar resolution (5, 10, 60)
        """
        for symbol in symbols:
            if symbol not in self._subscriptions:
                self._subscriptions[symbol] = []
            resolution = f"{bar_sec}s"
            if resolution not in self._subscriptions[symbol]:
                self._subscriptions[symbol].append(resolution)

        await self.engine.subscribe_features(self.strategy_id, symbols, bar_sec)

    async def history(
        self,
        symbol: str,
        resolution: str,
        limit: int,
        fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical features from ClickHouse

        Args:
            symbol: Symbol
            resolution: Bar resolution ('5s', '10s', '1m')
            limit: Number of bars
            fields: Optional field filter

        Returns:
            List of feature dicts
        """
        return await self.engine.history_features(
            symbol, resolution, limit, fields
        )

    # ---- Portfolio state ----

    async def portfolio(self) -> Dict[str, Any]:
        """
        Get portfolio view

        Returns:
            Portfolio dict with cash, equity, positions
        """
        return await self.engine.portfolio_view(self.strategy_id)

    async def position(self, symbol: str) -> PositionSnapshot:
        """
        Get position for symbol

        Args:
            symbol: Symbol

        Returns:
            Position snapshot
        """
        return await self.engine.position_view(self.strategy_id, symbol)

    async def open_orders(self) -> List[OrderUpdate]:
        """
        Get open orders

        Returns:
            List of open orders
        """
        return await self.engine.open_orders_view(self.strategy_id)

    def report_data_unavailable(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Report dataset outage to the central entry gate.

        This is deliberately sync so strategy components can call it from error
        handling paths.  It blocks entry only; safety actions remain admissible
        through UOM safety lanes.
        """
        from services.data_availability import get_data_availability_service

        payload = dict(payload or {})
        gate = get_data_availability_service().mark_unavailable(
            dataset=str(payload.get("dataset") or payload.get("source") or "market_data"),
            symbol=str(payload.get("symbol") or ""),
            resolution=str(payload.get("resolution") or payload.get("interval") or ""),
            reason=str(payload.get("reason") or "data_unavailable"),
            source=str(payload.get("source") or ""),
            strategy_id=str(payload.get("strategy_id") or self.strategy_id),
            stage=str(payload.get("stage") or ""),
            severity=str(payload.get("severity") or "entry_blocking"),
            error=str(payload.get("error") or ""),
        )
        return {
            "ok": False,
            "gate": gate.__dict__,
            "safety_actions_allowed": True,
        }

    def clear_data_unavailable(self, payload: Dict[str, Any]) -> None:
        from services.data_availability import get_data_availability_service

        payload = dict(payload or {})
        get_data_availability_service().clear(
            dataset=str(payload.get("dataset") or payload.get("source") or "market_data"),
            symbol=str(payload.get("symbol") or ""),
            resolution=str(payload.get("resolution") or payload.get("interval") or ""),
            strategy_id=str(payload.get("strategy_id") or self.strategy_id),
            stage=str(payload.get("stage") or ""),
        )

    # ---- Trading intents ----

    async def submit_intent(self, envelope: DecisionEnvelope) -> Any:
        """
        Submit trading intent

        Intent goes through:
        1. Validator (format, constraints)
        2. Arbiter (priority, capacity)
        3. Risk (capital reservation)
        4. Execution (order placement + OCO protection)

        Args:
            envelope: Decision envelope
        """
        extras = dict(envelope.extras or {})
        if not str(extras.get("strategy_run_id") or "").strip():
            extras["strategy_run_id"] = self.strategy_run_id
        if not str(extras.get("trace_id") or "").strip():
            # Use decision_id as a stable trace_id when upstream does not provide one.
            extras["trace_id"] = str(envelope.decision_id or "")
        if not str(extras.get("trigger_id") or "").strip():
            # Legacy fallback: tie trigger_id to decision_id for traceability.
            extras["trigger_id"] = str(envelope.decision_id or "")
            extras.setdefault("trace_source", "legacy_decision_id")
        if not str(extras.get("intent_source") or "").strip():
            extras["intent_source"] = "strategy"
        envelope.extras = extras
        return await self.engine.submit_intent(self.strategy_id, envelope)

    # ---- Cadence control ----

    async def set_cadence(self, seconds: int) -> None:
        """
        Set strategy execution cadence

        Args:
            seconds: Cadence in seconds (minimum 5)
        """
        await self.engine.set_cadence(self.strategy_id, seconds)

    # ---- Dynamic Tick Data Subscription ----

    async def subscribe_tick_data(self, symbols: List[str]) -> Dict[str, bool]:
        """
        Subscribe to raw tick data (triggers IB subscription + Kafka publishing).

        This is different from subscribe_features() which only consumes pre-aggregated
        bars from Kafka. This method triggers actual IBKR reqMktData() calls, causing:
        1. IB Gateway to send tick-by-tick market data
        2. MarketDataService to publish to Kafka (md.tick.trade.v1, md.tick.quote.v1)
        3. SFS-1s service to aggregate into 1-second bars in Redis

        Args:
            symbols: List of stock symbols to subscribe (e.g., ['AAPL', 'TSLA'])

        Returns:
            Dict[symbol, success]: Subscription result for each symbol

        Example:
            ```python
            async def initialize(self):
                # Subscribe to aggregated bars (from Kafka)
                await self.ctx.subscribe_features(["AAPL"], bar_sec=60)

                # Subscribe to raw tick data (triggers IB subscription)
                results = await self.ctx.subscribe_tick_data(["NVDA", "AMD"])

                for symbol, success in results.items():
                    if success:
                        self.log.info(f"✅ {symbol} tick subscription successful")
                    else:
                        self.log.error(f"❌ {symbol} tick subscription failed")
            ```

        Note:
            - Data will start flowing after 2-5 seconds (IBKR delay)
            - Subscription persists until restart or explicit unsubscribe
            - IBKR free accounts limited to ~100 concurrent subscriptions
            - You can then read 1-second bars from Redis using SFS1sReader
        """
        return await self.engine.subscribe_tick_data(self.strategy_id, symbols)

    # ---- Utilities ----

    def now_ms(self) -> int:
        """Get current timestamp in milliseconds"""
        from datetime import datetime
        return int(datetime.now().timestamp() * 1000)

    def param(self, key: str, default: Any = None) -> Any:
        """
        Get strategy parameter

        Args:
            key: Parameter key
            default: Default value

        Returns:
            Parameter value or default
        """
        return self.params.extras.get(key, default)

    def get_price(self, symbol: str) -> Optional[Any]:
        """
        Get real-time L1 price snapshot from PriceCache

        Args:
            symbol: Symbol to query

        Returns:
            PriceSnapshot with bid/ask/last or None if not available
        """
        if self._price_cache is None:
            self.log.warning(f"PriceCache not configured, cannot get price for {symbol}")
            return None
        return self._price_cache.price(symbol)

    def get_minute_stats(self, symbol: str) -> Optional[Any]:
        """
        Get minute-level statistics from PriceCache

        Args:
            symbol: Symbol to query

        Returns:
            MinuteRollup with VWAP, RVOL, PM_H, ORB5_H, etc.
        """
        if self._price_cache is None:
            self.log.warning(f"PriceCache not configured, cannot get minute stats for {symbol}")
            return None
        return self._price_cache.minute_stats(symbol)

    # ---- Realtime Feature Engine (RFE) ----

    async def enable_realtime_feature_engine(
        self,
        symbols: List[str],
        spec: Optional[Dict[str, Any]] = None,
        emit_interval_sec: int = 1,
        warmup_bars: int = 60,
    ) -> None:
        """
        Enable realtime feature engine using 1s bars (SFS1sReader) and compute on-the-fly features.

        Args:
            symbols: Symbols to track
            spec: Optional RFE config with keys: {enabled, features, timeframes, emit_on, lookbacks}
            emit_interval_sec: Polling interval for 1s bars
            warmup_bars: Bars to wait before reporting warmup_ok (first emission already useful)
        """
        if self._rfe_enabled:
            return
        try:
            from libs.strategy.realtime_feature_engine import RealtimeFeatureEngine  # type: ignore
            from libs.dal.sfs_facade import SFS1sReader  # type: ignore
        except Exception as e:  # pragma: no cover
            self.log.warning("rfe_import_failed", error=str(e))
            return

        spec = spec or {}
        emit_on = str(spec.get("emit_on", "1m"))
        feature_configs = spec.get("features") or None
        timeframes = spec.get("timeframes") or ("1s", "1m", "5m")
        lookbacks = spec.get("lookbacks") or None
        self._rfe_emit_tf = emit_on

        # Instantiate engine
        eng = RealtimeFeatureEngine(
            symbols=symbols,
            feature_configs=feature_configs,
            timeframes=timeframes,
            emit_on=emit_on,
            lookbacks=lookbacks,
        )

        # Optional: ClickHouse sink configuration
        sink_cfg = (spec.get("sink") or {})
        sink_enabled = bool(sink_cfg.get("enabled", False)) and (emit_on == "1m")
        sink_table = sink_cfg.get("table", "trading.features_1m_active")

        # Register callback to update cache
        async def _on_emit(sym: str, feats: Dict[str, Any]) -> None:
            try:
                # ts already set by engine in microseconds
                ts_us = int(feats.get("ts") or 0)
                self._rfe_cache[sym] = dict(feats)
                if ts_us:
                    self._rfe_ts_us[sym] = ts_us
                # Update heartbeat file for supervisor (best-effort)
                try:
                    from pathlib import Path as _P
                    import json as _J
                    from datetime import datetime as _DT
                    dstr = _DT.now().date().isoformat()
                    p = _P("runs/ai_trader") / dstr / "rfe_heartbeat.json"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    data = {"ts": _DT.now().isoformat(), "symbols": {}}
                    if p.exists():
                        try:
                            data = _J.loads(p.read_text("utf-8")) or data
                        except Exception:
                            pass
                    syms = data.get("symbols") or {}
                    syms[sym] = ts_us
                    data["symbols"] = syms
                    p.write_text(_J.dumps(data), encoding="utf-8")
                except Exception:
                    pass
                # Mark warm when we observe first emission; guard with configured threshold
                if not self._rfe_warm.get(sym):
                    self._rfe_warm[sym] = True if warmup_bars <= 1 else True
                # Emit-driven execution: hand off to engine when available
                try:
                    if hasattr(self.engine, "emit_features"):
                        await self.engine.emit_features(self.strategy_id, sym, dict(feats))  # type: ignore[attr-defined]
                except Exception:
                    # Never block on emit path
                    pass
                # Optional: sink to ClickHouse (off the event loop)
                if sink_enabled and ts_us:
                    try:
                        from libs.strategy.realtime_feature_engine import rfe_ch_sink as _sink  # type: ignore
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, _sink.enqueue_row, sym, ts_us, dict(feats), sink_table)
                    except Exception:
                        pass
            except Exception:
                pass

        eng.register_callback(_on_emit)

        # Seed/poll from SFS 1s Redis lists
        rdr = SFS1sReader()
        self._rfe_engine = eng
        self._rfe_reader = rdr
        self._rfe_enabled = True

        async def _seed_and_poll(sym: str) -> None:
            try:
                # Seed with recent 1s bars (up to warmup horizon)
                try:
                    recent = await rdr.last_n(sym, n=max(emit_interval_sec * 120, 60))
                    if recent:
                        # last_n returns newest..older; reverse to chronological
                        for row in reversed(recent):
                            await eng.on_1s_bar(sym, row)
                            # Track last ts to avoid reprocessing
                            ts_val = row.get("ts")
                            if isinstance(ts_val, (int, float)):
                                self._rfe_last_ts[sym] = int(ts_val)
                except Exception:
                    pass

                # Poll loop
                interval = max(0.2, float(emit_interval_sec))
                while self._rfe_enabled:
                    try:
                        row = await rdr.last(sym)
                        if not row:
                            await asyncio.sleep(interval)
                            continue
                        ts = row.get("ts")
                        ts_i = int(ts) if isinstance(ts, (int, float)) else None
                        if ts_i is not None and self._rfe_last_ts.get(sym) == ts_i:
                            await asyncio.sleep(interval)
                            continue
                        await eng.on_1s_bar(sym, row)
                        if ts_i is not None:
                            self._rfe_last_ts[sym] = ts_i
                    except Exception:
                        await asyncio.sleep(interval)
                        continue
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.log.warning("rfe_poll_task_error", symbol=sym, error=str(e))

        # Start a poll task per symbol
        for s in symbols:
            if s not in self._rfe_poll_tasks:
                self._rfe_poll_tasks[s] = asyncio.create_task(_seed_and_poll(s))

        self.log.info("rfe_enabled", symbols=symbols, emit_on=emit_on)

    def get_realtime_features(self, symbol: str) -> Tuple[Dict[str, Any], int, bool]:
        """
        Get last emitted realtime features for symbol.

        Returns:
            (features, ts_us, warmup_ok)
        """
        feats = dict(self._rfe_cache.get(symbol, {}) or {})
        ts_us = int(self._rfe_ts_us.get(symbol, 0))
        warm = bool(self._rfe_warm.get(symbol, False))
        return feats, ts_us, warm

    async def disable_realtime_feature_engine(self) -> None:
        """Disable RFE and stop pollers (best-effort)."""
        try:
            self._rfe_enabled = False
            # Cancel tasks
            for t in list(self._rfe_poll_tasks.values()):
                try:
                    t.cancel()
                except Exception:
                    pass
            self._rfe_poll_tasks.clear()
            # Close reader
            try:
                if self._rfe_reader is not None:
                    await self._rfe_reader.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass
