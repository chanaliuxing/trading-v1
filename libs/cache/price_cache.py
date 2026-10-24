"""
PriceCache - Low-latency in-memory market data cache

Consumes Kafka L1 ticks (~250ms latency) and maintains:
- Real-time price snapshots (bid/ask/last)
- 1m/5m minute rollups with VWAP, RVOL
- Premarket high/low (04:00-09:30 ET)
- Opening Range Breakout 5min high/low (09:30-09:35 ET)

Usage:
    cache = PriceCache()
    await cache.handle_tick(tick)

    # Query from strategy
    snapshot = cache.price("TSLA")
    stats = cache.minute_stats("TSLA")
"""

from __future__ import annotations

import asyncio
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, Optional, Deque
import time as time_module

import numpy as np
from zoneinfo import ZoneInfo

from libs.logger import get_logger

logger = get_logger(__name__)

# Timezone constants
ET = ZoneInfo("America/New_York")


@dataclass
class Tick:
    """L1 tick from Kafka/IB market data"""
    symbol: str
    last: float
    bid: float
    ask: float
    last_size: int = 0
    bid_size: int = 0
    ask_size: int = 0
    ts_us: int = 0  # Microseconds timestamp

    @property
    def spread_pct(self) -> float:
        """Half-spread as percentage"""
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / (self.ask + self.bid)
        return 0.0


@dataclass
class PriceSnapshot:
    """Real-time price snapshot"""
    symbol: str
    last: float
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    ts_us: int

    @property
    def spread_pct(self) -> float:
        """Half-spread as percentage"""
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / (self.ask + self.bid)
        return 0.0

    @property
    def mid(self) -> float:
        """Mid price"""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last


@dataclass
class MinuteRollup:
    """Minute-level aggregated statistics"""
    symbol: str
    vwap: float = 0.0           # Today's VWAP (from 09:30 ET)
    vol_1m: int = 0             # Current 1m volume
    rvol_1m: float = 1.0        # Relative volume 1m
    rvol_5m: float = 1.0        # Relative volume 5m
    pm_high: float = 0.0        # Premarket high (04:00-09:30 ET)
    pm_low: float = 0.0         # Premarket low (04:00-09:30 ET)
    orb5_high: float = 0.0      # ORB5 high (09:30-09:35 ET)
    orb5_low: float = 0.0       # ORB5 low (09:30-09:35 ET)
    spread_pct: float = 0.0     # Current spread
    last_update_us: int = 0


@dataclass
class MinuteBar:
    """Internal 1-minute bar for aggregation"""
    ts: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    pv: float = 0.0  # Price * Volume for VWAP


class PriceCache:
    """
    In-memory price cache with minute-level aggregations

    Features:
    - Real-time L1 snapshots (~250ms latency)
    - 1m/5m rolling aggregations
    - VWAP (from market open 09:30)
    - RVOL (relative volume vs historical same-minute)
    - PM_H/L (premarket high/low)
    - ORB5_H/L (opening range breakout 5min high/low)
    """

    def __init__(
        self,
        rvol_lookback_days: int = 60,
        max_1m_bars: int = 390,     # 1 trading day
        max_5m_bars: int = 78       # 1 trading day
    ):
        """
        Initialize PriceCache

        Args:
            rvol_lookback_days: Days to look back for RVOL calculation
            max_1m_bars: Maximum 1m bars to keep per symbol
            max_5m_bars: Maximum 5m bars to keep per symbol
        """
        self.rvol_lookback_days = rvol_lookback_days
        self.max_1m_bars = max_1m_bars
        self.max_5m_bars = max_5m_bars

        # L1 snapshots
        self._snapshots: Dict[str, PriceSnapshot] = {}

        # Minute bars (1m and 5m)
        self._bars_1m: Dict[str, Deque[MinuteBar]] = defaultdict(lambda: deque(maxlen=max_1m_bars))
        self._bars_5m: Dict[str, Deque[MinuteBar]] = defaultdict(lambda: deque(maxlen=max_5m_bars))

        # Current building bars
        self._current_1m: Dict[str, MinuteBar] = {}
        self._current_5m: Dict[str, MinuteBar] = {}

        # VWAP accumulators (reset daily at 09:30 ET)
        self._vwap_pv: Dict[str, float] = defaultdict(float)  # Cumulative price*volume
        self._vwap_v: Dict[str, float] = defaultdict(float)   # Cumulative volume

        # Historical volume for RVOL (keyed by symbol -> minute_of_day -> volumes)
        self._rvol_hist: Dict[str, Dict[int, Deque[float]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=rvol_lookback_days))
        )

        # Premarket/ORB tracking
        self._pm_high: Dict[str, float] = {}
        self._pm_low: Dict[str, float] = {}
        self._orb5_high: Dict[str, float] = {}
        self._orb5_low: Dict[str, float] = {}

        # Day tracking for reset
        self._current_date: Optional[str] = None

        # Subscribed symbols
        self._subscribed: set[str] = set()

        logger.info("PriceCache initialized",
                   rvol_lookback=rvol_lookback_days,
                   max_1m_bars=max_1m_bars,
                   max_5m_bars=max_5m_bars)

    def subscribe(self, symbols: list[str]) -> None:
        """
        Subscribe to symbols for tick aggregation

        Args:
            symbols: List of symbols to track
        """
        for symbol in symbols:
            if symbol not in self._subscribed:
                self._subscribed.add(symbol)
                logger.info(f"PriceCache subscribed to {symbol}")

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from symbol"""
        if symbol in self._subscribed:
            self._subscribed.discard(symbol)
            logger.info(f"PriceCache unsubscribed from {symbol}")

    async def handle_tick(self, tick: Tick) -> None:
        """
        Process incoming tick and update aggregations

        Args:
            tick: Incoming L1 tick
        """
        if tick.symbol not in self._subscribed:
            return

        # Update snapshot
        self._snapshots[tick.symbol] = PriceSnapshot(
            symbol=tick.symbol,
            last=tick.last,
            bid=tick.bid,
            ask=tick.ask,
            bid_size=tick.bid_size,
            ask_size=tick.ask_size,
            ts_us=tick.ts_us
        )

        # Get current time in ET
        now_et = datetime.now(ET)
        self._check_daily_reset(now_et)

        # Update minute bars
        self._update_minute_bars(tick, now_et)

        # Update premarket/ORB tracking
        self._update_session_tracking(tick, now_et)

    def _check_daily_reset(self, now_et: datetime) -> None:
        """Check if we need to reset for new trading day"""
        current_date = now_et.strftime("%Y-%m-%d")

        if self._current_date != current_date:
            # New day - reset VWAP accumulators and PM/ORB tracking
            logger.info(f"Daily reset: {self._current_date} -> {current_date}")
            self._current_date = current_date
            self._vwap_pv.clear()
            self._vwap_v.clear()
            self._pm_high.clear()
            self._pm_low.clear()
            self._orb5_high.clear()
            self._orb5_low.clear()

    def _update_minute_bars(self, tick: Tick, now_et: datetime) -> None:
        """Update 1m and 5m bars with tick"""
        symbol = tick.symbol

        # Round to minute
        minute_ts = now_et.replace(second=0, microsecond=0)

        # Update 1m bar
        if symbol not in self._current_1m or self._current_1m[symbol].ts != minute_ts:
            # Finalize previous bar
            if symbol in self._current_1m:
                self._finalize_bar(symbol, self._current_1m[symbol], "1m")

            # Start new bar
            self._current_1m[symbol] = MinuteBar(
                ts=minute_ts,
                open=tick.last,
                high=tick.last,
                low=tick.last,
                close=tick.last,
                volume=tick.last_size,
                pv=tick.last * tick.last_size
            )
        else:
            # Update existing bar
            bar = self._current_1m[symbol]
            bar.high = max(bar.high, tick.last)
            bar.low = min(bar.low, tick.last)
            bar.close = tick.last
            bar.volume += tick.last_size
            bar.pv += tick.last * tick.last_size

        # Update 5m bar (round to 5-minute boundary)
        minute_5 = minute_ts.replace(minute=(minute_ts.minute // 5) * 5)

        if symbol not in self._current_5m or self._current_5m[symbol].ts != minute_5:
            # Finalize previous bar
            if symbol in self._current_5m:
                self._finalize_bar(symbol, self._current_5m[symbol], "5m")

            # Start new bar
            self._current_5m[symbol] = MinuteBar(
                ts=minute_5,
                open=tick.last,
                high=tick.last,
                low=tick.last,
                close=tick.last,
                volume=tick.last_size,
                pv=tick.last * tick.last_size
            )
        else:
            # Update existing bar
            bar = self._current_5m[symbol]
            bar.high = max(bar.high, tick.last)
            bar.low = min(bar.low, tick.last)
            bar.close = tick.last
            bar.volume += tick.last_size
            bar.pv += tick.last * tick.last_size

    def _finalize_bar(self, symbol: str, bar: MinuteBar, resolution: str) -> None:
        """Finalize a completed minute bar and add to history"""
        if resolution == "1m":
            self._bars_1m[symbol].append(bar)

            # Update RVOL history (store volume for this minute-of-day)
            if bar.ts.time() >= time(9, 30):  # Only track RTH
                minute_of_day = bar.ts.hour * 60 + bar.ts.minute
                self._rvol_hist[symbol][minute_of_day].append(float(bar.volume))

            # Update VWAP (only during RTH, 09:30-16:00)
            if time(9, 30) <= bar.ts.time() <= time(16, 0):
                self._vwap_pv[symbol] += bar.pv
                self._vwap_v[symbol] += bar.volume

        elif resolution == "5m":
            self._bars_5m[symbol].append(bar)

        logger.debug(f"Finalized {resolution} bar",
                    symbol=symbol,
                    ts=bar.ts.isoformat(),
                    ohlcv=f"{bar.open:.2f}/{bar.high:.2f}/{bar.low:.2f}/{bar.close:.2f}/{bar.volume}")

    def _update_session_tracking(self, tick: Tick, now_et: datetime) -> None:
        """Update premarket and ORB tracking"""
        symbol = tick.symbol
        current_time = now_et.time()

        # Premarket high/low (04:00-09:30 ET)
        if time(4, 0) <= current_time < time(9, 30):
            if symbol not in self._pm_high or tick.last > self._pm_high[symbol]:
                self._pm_high[symbol] = tick.last
            if symbol not in self._pm_low or tick.last < self._pm_low[symbol]:
                self._pm_low[symbol] = tick.last

        # ORB5 high/low (09:30-09:35 ET)
        if time(9, 30) <= current_time < time(9, 35):
            if symbol not in self._orb5_high or tick.last > self._orb5_high[symbol]:
                self._orb5_high[symbol] = tick.last
            if symbol not in self._orb5_low or tick.last < self._orb5_low[symbol]:
                self._orb5_low[symbol] = tick.last

    def price(self, symbol: str) -> Optional[PriceSnapshot]:
        """
        Get latest L1 price snapshot

        Args:
            symbol: Symbol to query

        Returns:
            PriceSnapshot or None if not available
        """
        return self._snapshots.get(symbol)

    def minute_stats(self, symbol: str) -> MinuteRollup:
        """
        Get minute-level statistics

        Args:
            symbol: Symbol to query

        Returns:
            MinuteRollup with VWAP, RVOL, PM/ORB levels
        """
        snapshot = self._snapshots.get(symbol)

        # Calculate VWAP
        vwap = 0.0
        if symbol in self._vwap_v and self._vwap_v[symbol] > 0:
            vwap = self._vwap_pv[symbol] / self._vwap_v[symbol]

        # Calculate RVOL
        rvol_1m = self._calculate_rvol(symbol, "1m")
        rvol_5m = self._calculate_rvol(symbol, "5m")

        # Get current bar volumes
        vol_1m = 0
        if symbol in self._current_1m:
            vol_1m = self._current_1m[symbol].volume

        # Get PM/ORB levels
        pm_high = self._pm_high.get(symbol, 0.0)
        pm_low = self._pm_low.get(symbol, 0.0)
        orb5_high = self._orb5_high.get(symbol, 0.0)
        orb5_low = self._orb5_low.get(symbol, 0.0)

        # Spread
        spread_pct = snapshot.spread_pct if snapshot else 0.0

        return MinuteRollup(
            symbol=symbol,
            vwap=vwap,
            vol_1m=vol_1m,
            rvol_1m=rvol_1m,
            rvol_5m=rvol_5m,
            pm_high=pm_high,
            pm_low=pm_low,
            orb5_high=orb5_high,
            orb5_low=orb5_low,
            spread_pct=spread_pct,
            last_update_us=snapshot.ts_us if snapshot else 0
        )

    def _calculate_rvol(self, symbol: str, resolution: str) -> float:
        """
        Calculate relative volume

        Args:
            symbol: Symbol
            resolution: "1m" or "5m"

        Returns:
            RVOL (current volume / historical average for same minute)
        """
        now_et = datetime.now(ET)
        current_time = now_et.time()

        # Only calculate during RTH
        if current_time < time(9, 30) or current_time >= time(16, 0):
            return 1.0

        minute_of_day = now_et.hour * 60 + now_et.minute

        if resolution == "1m":
            # Current 1m volume
            current_vol = 0
            if symbol in self._current_1m:
                current_vol = self._current_1m[symbol].volume

            # Historical average for this minute
            if symbol in self._rvol_hist and minute_of_day in self._rvol_hist[symbol]:
                hist_vols = list(self._rvol_hist[symbol][minute_of_day])
                if hist_vols:
                    avg_vol = np.median(hist_vols)
                    if avg_vol > 0:
                        return current_vol / avg_vol

            return 1.0

        elif resolution == "5m":
            # Sum last 5 minutes of volume
            current_vol_5m = 0
            if len(self._bars_1m[symbol]) >= 5:
                for bar in list(self._bars_1m[symbol])[-5:]:
                    current_vol_5m += bar.volume

            # Add current building bar
            if symbol in self._current_1m:
                current_vol_5m += self._current_1m[symbol].volume

            # Historical average for same 5-minute window
            # Simplified: use median of recent same-slot 5m volumes
            # (Full implementation would track 5m historical volumes)
            # For now, approximate as 5x the 1m RVOL
            rvol_1m = self._calculate_rvol(symbol, "1m")
            return rvol_1m

        return 1.0

    def get_bars_1m(self, symbol: str, limit: int = 100) -> list[MinuteBar]:
        """Get recent 1m bars"""
        if symbol not in self._bars_1m:
            return []
        bars = list(self._bars_1m[symbol])
        return bars[-limit:] if len(bars) > limit else bars

    def get_bars_5m(self, symbol: str, limit: int = 100) -> list[MinuteBar]:
        """Get recent 5m bars"""
        if symbol not in self._bars_5m:
            return []
        bars = list(self._bars_5m[symbol])
        return bars[-limit:] if len(bars) > limit else bars
