"""
Strategy Base Classes
Based on doc/8.md and doc/59.md

Provides the foundation for strategy development with:
- StrategyAlgorithm base class
- StrategyContext API
- Event-driven lifecycle
- Data access abstractions
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime
import uuid

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StrategyParams:
    """Strategy configuration parameters"""
    cadence_s: int = 5  # Strategy execution interval (seconds)
    ttl_ms: int = 2000  # Intent TTL (milliseconds)
    qscore_threshold: int = 70  # Minimum quality score
    max_notional_usd: float = 100000  # Max capital per strategy
    per_symbol_max_usd: float = 50000  # Max capital per symbol
    risk_per_trade_usd: float = 2000  # Risk budget per trade
    oco: Optional[Dict[str, Any]] = None  # OCO configuration
    extras: Dict[str, Any] = field(default_factory=dict)  # Custom params


@dataclass
class Freshness:
    """Intent freshness/TTL configuration"""
    ttl_ms: int  # Time-to-live in milliseconds
    dlb_ms: int = 500  # Decision lag budget


@dataclass
class OrderStyle:
    """Order type and pricing"""
    kind: str  # MKT/LMT/STP/STP_LMT/TRAIL/TRAIL_LIMIT
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
    tif: str = 'DAY'  # Time in force
    outside_rth: Optional[bool] = None


@dataclass
class DecisionEnvelope:
    """
    High-level trading intent from strategy

    This is what strategies produce. The execution pipeline
    (Validator → Arbiter → Risk → Executor) transforms it
    into actual orders.
    """
    decision_id: str
    strategy_id: str
    symbol: str
    ts_bar_end_us: int  # Bar end timestamp (microseconds)
    target_qty: float  # Target position (positive=long, negative=short)
    order_style: OrderStyle
    freshness: Freshness
    extras: Dict[str, Any] = field(default_factory=dict)  # ref_px, edge_bp, etc.

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            'decision_id': self.decision_id,
            'strategy_id': self.strategy_id,
            'symbol': self.symbol,
            'ts_bar_end_us': self.ts_bar_end_us,
            'target_qty': self.target_qty,
            'order_style': {
                'kind': self.order_style.kind,
                'limit_px': self.order_style.limit_px,
                'stop_px': self.order_style.stop_px,
                'trail_amount': self.order_style.trail_amount,
                'trail_percent': self.order_style.trail_percent,
                'trail_stop_px': self.order_style.trail_stop_px,
                'lmt_price_offset': self.order_style.lmt_price_offset,
                'protective_stop': self.order_style.protective_stop,
                'close_only': self.order_style.close_only,
                'reduce_only': self.order_style.reduce_only,
                'role': self.order_style.role,
                'tif': self.order_style.tif,
                'outside_rth': self.order_style.outside_rth,
            },
            'freshness': {
                'ttl_ms': self.freshness.ttl_ms,
                'dlb_ms': self.freshness.dlb_ms
            },
            'extras': self.extras
        }


class StrategyContext:
    """
    Context provided to strategies by the engine

    This is the strategy's API to:
    - Access market data and features
    - Submit trading intents
    - Query portfolio state
    - Configure execution
    """

    def __init__(
        self,
        engine: 'StrategyEngine',
        strategy_id: str,
        logger_func: Optional[Callable] = None,
        metrics_func: Optional[Callable] = None,
        price_cache: Optional[Any] = None
    ):
        self.engine = engine
        self.strategy_id = strategy_id
        self.log = logger_func or logger
        self.metrics = metrics_func or (lambda: {})
        self._subscriptions: Dict[str, List[int]] = {}  # symbol -> [resolutions]
        self._price_cache = price_cache  # PriceCache instance for low-latency data
        # Stable per-run identifier for traceability.
        self.strategy_run_id = str(uuid.uuid4())

    async def subscribe_features(
        self,
        symbols: List[str],
        bar_sec: int = 5
    ) -> None:
        """Subscribe to feature updates"""
        for symbol in symbols:
            if symbol not in self._subscriptions:
                self._subscriptions[symbol] = []
            if bar_sec not in self._subscriptions[symbol]:
                self._subscriptions[symbol].append(bar_sec)

        await self.engine.subscribe_features(self.strategy_id, symbols, bar_sec)

    async def history(
        self,
        symbol: str,
        bar_sec: int,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        fields: Optional[List[str]] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch historical features from ClickHouse"""
        return await self.engine.history_features(
            symbol, bar_sec, start_ts, end_ts, fields, limit
        )

    async def set_cadence(self, seconds: int) -> None:
        """Set strategy execution cadence"""
        await self.engine.set_cadence(self.strategy_id, seconds)

    async def portfolio(self) -> Dict[str, Any]:
        """Get portfolio snapshot"""
        return await self.engine.portfolio_view(self.strategy_id)

    async def position(self, symbol: str) -> Dict[str, Any]:
        """Get position for symbol"""
        return await self.engine.position_view(self.strategy_id, symbol)

    async def submit_intent(self, envelope: DecisionEnvelope) -> Any:
        """Submit trading intent to decision pipeline"""
        extras = dict(envelope.extras or {})
        if not str(extras.get("strategy_run_id") or "").strip():
            extras["strategy_run_id"] = self.strategy_run_id
        if not str(extras.get("trace_id") or "").strip():
            extras["trace_id"] = str(envelope.decision_id or "")
        if not str(extras.get("trigger_id") or "").strip():
            extras["trigger_id"] = str(envelope.decision_id or "")
            extras.setdefault("trace_source", "legacy_decision_id")
        if not str(extras.get("intent_source") or "").strip():
            extras["intent_source"] = "strategy"
        envelope.extras = extras
        return await self.engine.submit_intent(self.strategy_id, envelope)

    def now_ms(self) -> int:
        """Current timestamp in milliseconds"""
        import time
        return int(time.time() * 1000)

    def now_us(self) -> int:
        """Current timestamp in microseconds"""
        import time
        return int(time.time() * 1_000_000)

    def param(self, key: str, default=None) -> Any:
        """Get strategy parameter"""
        return self.engine.get_param(self.strategy_id, key, default)

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


class StrategyAlgorithm:
    """
    Base class for all trading strategies

    Strategies inherit from this and implement:
    - initialize(): One-time setup
    - on_features(): Core logic triggered on new data
    - on_order_update(): React to order events
    - on_position_update(): React to position changes
    - on_shutdown(): Cleanup

    Example:
        class MyStrategy(StrategyAlgorithm):
            async def initialize(self):
                await self.ctx.subscribe_features(['AAPL'], bar_sec=5)

            async def on_features(self, symbol: str, features: dict):
                if features['zscore_20'] < -2.0:
                    # Mean reversion signal
                    envelope = DecisionEnvelope(...)
                    await self.ctx.submit_intent(envelope)
    """

    def __init__(self, ctx: StrategyContext, params: StrategyParams):
        self.ctx = ctx
        self.params = params
        self.state: Dict[str, Any] = {}  # Persistent state
        self.log = ctx.log
        self.metrics = ctx.metrics

    async def initialize(self) -> None:
        """
        Initialize strategy (called once at startup)

        Use this to:
        - Subscribe to symbols and data
        - Set cadence
        - Load historical state
        - Configure parameters

        Time budget: <300ms
        """
        pass

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        """
        Main strategy logic (called on new features)

        Args:
            symbol: Symbol for this update
            features: Feature dictionary (close, zscore, atr, etc.)

        Time budget: P95 < 200ms
        """
        pass

    async def on_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """
        Optional: React to raw bar data

        Args:
            symbol: Symbol
            bar: Bar data (open, high, low, close, volume)

        Time budget: P95 < 200ms
        """
        pass

    async def on_order_update(self, update: Dict[str, Any]) -> None:
        """
        React to order status changes

        Args:
            update: Order update (state, fills, etc.)

        Time budget: <50ms
        """
        pass

    async def on_position_update(self, position: Dict[str, Any]) -> None:
        """
        React to position changes

        Args:
            position: Position snapshot (qty, avg_px, pnl)

        Time budget: <50ms
        """
        pass

    async def on_shutdown(self) -> None:
        """
        Cleanup on strategy shutdown

        Time budget: <200ms
        """
        pass

    async def on_error(self, err: Exception, where: str) -> None:
        """
        Handle errors in strategy code

        Args:
            err: Exception that occurred
            where: Location (callback name)
        """
        self.log.error(f"Strategy error in {where}", error=str(err), exc_info=True)


# Helper functions for strategy development

def make_decision_id() -> str:
    """Generate unique decision ID"""
    return str(uuid.uuid4())


def calculate_position_size(
    notional_usd: float,
    price: float,
    lot_size: int = 1
) -> float:
    """
    Calculate position size from notional and price

    Args:
        notional_usd: Dollar amount to allocate
        price: Current price
        lot_size: Minimum lot size (default 1 share)

    Returns:
        Position size in shares
    """
    if price <= 0:
        return 0.0

    shares = notional_usd / price
    # Round to nearest lot
    import math
    return math.floor(shares / lot_size + 0.5) * lot_size


def atr_position_size(
    risk_usd: float,
    atr: float,
    atr_multiplier: float = 2.0,
    price: float = None
) -> float:
    """
    Calculate position size based on ATR risk

    Args:
        risk_usd: Risk budget in USD
        atr: Average True Range
        atr_multiplier: ATR multiplier for stop
        price: Current price (optional, for validation)

    Returns:
        Position size in shares
    """
    if atr <= 0:
        return 0.0

    stop_distance = atr * atr_multiplier
    shares = risk_usd / stop_distance

    return round(shares)
