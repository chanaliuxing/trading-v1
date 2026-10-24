"""
Market State Provider - NBBO + Regulatory State Cache

Provides real-time market state for:
- NBBO (bid/ask/last/mid)
- Regulatory state (LULD, SSR, Halted)
- Tick size and lot size
- Multi-source fallback (IB -> Exchange -> Bar-derived)

Based on doc/46.md specification.
"""
import time
import asyncio
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Iterable, Protocol
from abc import ABC, abstractmethod

from libs.logger import get_logger

logger = get_logger(__name__)


def _now_us() -> int:
    """Current timestamp in microseconds"""
    return int(time.time() * 1e6)


@dataclass
class NBBOSnapshot:
    """
    Snapshot of NBBO and market state for a symbol
    """
    symbol: str
    ts_us: int = 0
    bid: Optional[float] = None
    bid_size: Optional[float] = None
    ask: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    last_size: Optional[float] = None
    mid: Optional[float] = None
    tick: float = 0.01  # Minimum price increment
    lot: float = 1.0  # Minimum lot size
    # Regulatory status (can be patched from external sources)
    luld_low: Optional[float] = None
    luld_high: Optional[float] = None
    ssr_active: bool = False
    halted: bool = False
    # Source tracking (for debugging)
    sources: Dict[str, int] = None  # {"ib": ts_us, "patch": ts_us, "xchg": ts_us}

    def to_market_state_dict(self) -> Dict[str, Any]:
        """Convert to market state dict for MarketStateProvider"""
        d = asdict(self)
        # Only return key fields needed by MarketStateProvider
        return {
            "symbol": self.symbol,
            "ts_us": self.ts_us,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "mid": self.mid if self.mid is not None and not (self.mid != self.mid) else None,
            "tick": self.tick,
            "lot": self.lot,
            "luld_low": self.luld_low,
            "luld_high": self.luld_high,
            "ssr": self.ssr_active,
            "halted": self.halted,
        }


@dataclass
class MarketState:
    """
    Unified market state object
    """
    symbol: str
    ts_us: int
    bid_px: Optional[float] = None
    ask_px: Optional[float] = None
    mid_px: Optional[float] = None
    last_px: Optional[float] = None
    luld_low: Optional[float] = None
    luld_high: Optional[float] = None
    ssr_active: bool = False
    halted: bool = False
    tick_size: float = 0.01
    lot_size: float = 1.0
    source: str = "unknown"


class MarketStateSource(Protocol):
    """Protocol for market state sources"""

    async def get(self, symbol: str) -> Optional[MarketState]:
        """Get market state for symbol"""
        ...


class NBBOSnapshotCache:
    """
    NBBO Snapshot Cache with TTL and concurrent safety

    Features:
    - Collects IB ticker updates into readable snapshots
    - Allows external status patches (LULD/SSR/Halt from exchange/regulatory sources)
    - TTL-based staleness detection
    - Thread-safe async operations
    """

    def __init__(self, ttl_ms: int = 800):
        self.ttl_ms = ttl_ms
        self._lock = asyncio.Lock()
        self._snap: Dict[str, NBBOSnapshot] = {}
        self._subscribed: set[str] = set()
        self._closed = False

        # Metrics
        self.metrics = type(
            "M",
            (),
            {
                "nbbo_updates_total": 0,
                "status_patches_total": 0,
                "reads_total": 0,
                "reads_stale_total": 0,
            },
        )()

    async def read(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Read cached snapshot. Returns None if stale (> TTL).
        Caller should fallback to other sources.
        """
        async with self._lock:
            self.metrics.reads_total += 1
            s = self._snap.get(symbol)
            if not s:
                self.metrics.reads_stale_total += 1
                return None
            if (_now_us() - s.ts_us) > self.ttl_ms * 1000:
                self.metrics.reads_stale_total += 1
                return None
            # Derive mid if not set
            if s.mid is None and s.bid is not None and s.ask is not None:
                s.mid = (s.bid + s.ask) / 2.0
            return s.to_market_state_dict()

    async def update(self, symbol: str, **fields) -> None:
        """
        Manually update snapshot
        Available fields: bid, bid_size, ask, ask_size, last, last_size, tick, lot
        """
        if self._closed:
            return
        async with self._lock:
            s = self._snap.get(symbol) or NBBOSnapshot(symbol=symbol, sources={})
            changed = False
            for k, v in fields.items():
                if not hasattr(s, k):
                    continue
                if v is None:
                    continue
                # Type conversion for numeric fields
                if isinstance(getattr(s, k, None), (float, type(None))) and isinstance(
                    v, (int, float)
                ):
                    setattr(s, k, float(v))
                else:
                    setattr(s, k, v)
                changed = True
            if changed:
                s.ts_us = _now_us()
                s.sources = s.sources or {}
                s.sources["manual"] = s.ts_us
                # Update mid
                if s.bid is not None and s.ask is not None:
                    s.mid = (s.bid + s.ask) / 2.0
                self._snap[symbol] = s
                self.metrics.nbbo_updates_total += 1

    async def apply_status(self, symbol: str, **status_fields) -> None:
        """
        Apply external status patch (exchange/regulatory/internal service)
        Available fields: luld_low, luld_high, ssr_active, halted, tick, lot
        """
        if self._closed:
            return
        async with self._lock:
            s = self._snap.get(symbol) or NBBOSnapshot(symbol=symbol, sources={})
            for k in ("luld_low", "luld_high", "ssr_active", "halted", "tick", "lot"):
                if k in status_fields and status_fields[k] is not None:
                    setattr(s, k, status_fields[k])
            s.ts_us = s.ts_us or _now_us()  # Set timestamp if not already set
            s.sources = s.sources or {}
            s.sources["patch"] = _now_us()
            self._snap[symbol] = s
            self.metrics.status_patches_total += 1

    async def close(self):
        """Close cache and cleanup"""
        self._closed = True

    # Optional: Direct IB-insync integration
    async def subscribe_ib(
        self,
        ib,
        symbols: Iterable[str],
        generic_ticks: str = "",
        snapshot: bool = False,
    ):
        """
        Subscribe to IB market data using ib-insync

        Args:
            ib: IB connection object
            symbols: List of symbols to subscribe
            generic_ticks: Generic tick list (IB format)
            snapshot: If True, only fetch snapshot once
        """
        if self._closed:
            return
        todo = [s for s in symbols if s not in self._subscribed]
        if not todo:
            return

        from ib_insync import Stock, Index

        contracts = []
        for sym in todo:
            if sym == "VIX":
                contracts.append(Index("VIX", "CBOE", "USD"))
            else:
                contracts.append(Stock(sym, "SMART", "USD"))
        qualified = await ib.qualifyContractsAsync(*contracts)

        for sym, contract in zip(todo, qualified):
            ticker = ib.reqMktData(contract, genericTickList=generic_ticks, snapshot=snapshot)
            # Bind symbol for callback identification
            setattr(ticker, "_q_symbol", sym)
            ticker.updateEvent += self._on_ib_update
            self._subscribed.add(sym)

    def _on_ib_update(self, ticker):
        """
        IB-insync Ticker.updateEvent callback
        Extracts NBBO fields and updates cache asynchronously
        """
        try:
            sym = getattr(ticker, "_q_symbol", None) or (
                getattr(ticker.contract, "symbol", None) or ""
            )
            if not sym:
                return

            # Sample latest snapshot
            fields = {}
            if getattr(ticker, "bid", None) not in (None, float("nan")):
                fields["bid"] = float(ticker.bid)
            if getattr(ticker, "ask", None) not in (None, float("nan")):
                fields["ask"] = float(ticker.ask)
            if getattr(ticker, "last", None) not in (None, float("nan")):
                fields["last"] = float(ticker.last)
            if getattr(ticker, "bidSize", None):
                fields["bid_size"] = float(ticker.bidSize)
            if getattr(ticker, "askSize", None):
                fields["ask_size"] = float(ticker.askSize)
            if getattr(ticker, "lastSize", None):
                fields["last_size"] = float(ticker.lastSize)

            # Update cache asynchronously to avoid blocking IB event loop
            asyncio.get_event_loop().create_task(self.update(sym, **fields))
        except Exception as e:
            logger.warning(f"IB update error: {e}")


class MarketStateIB:
    """Market state source from IB NBBO cache"""

    def __init__(self, nbbo_cache: NBBOSnapshotCache):
        self.cache = nbbo_cache

    async def get(self, symbol: str) -> Optional[MarketState]:
        """Get market state from IB cache"""
        s = await self.cache.read(symbol)
        if not s:
            return None
        return MarketState(
            symbol=s["symbol"],
            ts_us=s["ts_us"],
            bid_px=s["bid"],
            ask_px=s["ask"],
            last_px=s["last"],
            mid_px=s["mid"],
            luld_low=s["luld_low"],
            luld_high=s["luld_high"],
            ssr_active=bool(s["ssr"]),
            halted=bool(s["halted"]),
            tick_size=s["tick"],
            lot_size=s["lot"],
            source="ib",
        )


class MarketStateProvider:
    """
    Multi-source market state provider with fallback

    Sources (in priority order):
    1. Primary: Exchange authoritative source
    2. Secondary: IB NBBO cache
    3. Fallback: Bar-derived estimates

    All sources respect TTL for staleness detection.
    """

    def __init__(
        self,
        primary: Optional[MarketStateSource] = None,
        secondary: Optional[MarketStateSource] = None,
        fallback: Optional[MarketStateSource] = None,
        ttl_ms: int = 800,
    ):
        self.primary = primary
        self.secondary = secondary
        self.fallback = fallback
        self.ttl_ms = ttl_ms

        # Metrics
        self.metrics = type(
            "M",
            (),
            {
                "get_total": 0,
                "primary_hits": 0,
                "secondary_hits": 0,
                "fallback_hits": 0,
                "misses": 0,
            },
        )()

    async def get(self, symbol: str) -> Optional[MarketState]:
        """
        Get market state for symbol with multi-source fallback

        Returns:
            MarketState if available from any source, None otherwise
        """
        self.metrics.get_total += 1

        # Try primary
        if self.primary:
            state = await self.primary.get(symbol)
            if state and self._is_fresh(state):
                self.metrics.primary_hits += 1
                return state

        # Try secondary
        if self.secondary:
            state = await self.secondary.get(symbol)
            if state and self._is_fresh(state):
                self.metrics.secondary_hits += 1
                return state

        # Try fallback
        if self.fallback:
            state = await self.fallback.get(symbol)
            if state and self._is_fresh(state):
                self.metrics.fallback_hits += 1
                return state

        # No fresh data available
        self.metrics.misses += 1
        return None

    def _is_fresh(self, state: MarketState) -> bool:
        """Check if state is fresh (within TTL)"""
        age_us = _now_us() - state.ts_us
        return age_us <= self.ttl_ms * 1000
