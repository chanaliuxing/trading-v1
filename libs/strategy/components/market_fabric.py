"""
MarketFabric Component
Handles real-time data ingestion with Strict RTH Boundaries and Clock Calibration.
"""
import asyncio
from typing import Any, Dict, List, Optional, Deque, Iterable
from collections import deque
from datetime import datetime, timedelta, time
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from itertools import islice
from .base import StrategyComponent
from libs.dal.sfs_facade import SFS1sReader

ET = ZoneInfo("America/New_York")

@dataclass
class OhlcvBar:
    ts_start: datetime; ts_end: datetime
    open: float; high: float; low: float; close: float; volume: float; pv_sum: float = 0.0 

class MarketFabric(StrategyComponent):
    def __init__(self, ctx: Any, state: Dict[str, Any], state_lock: asyncio.Lock, symbols: List[str], max_len: int = 5000):
        super().__init__(ctx, state, state_lock)
        self.symbols = symbols
        self._sfs: Optional[SFS1sReader] = None
        self._last_seen_ts_ms: Dict[str, int] = {s: 0 for s in symbols}
        self._last_ingest_at_ms: Dict[str, int] = {s: 0 for s in symbols}
        self._logic_watermark_ms: Dict[str, int] = {s: 0 for s in symbols}
        self._time_offset_ms: float = 0.0
        self._calibrated: bool = False
        
        self.bars_1m: Dict[str, Deque[OhlcvBar]] = {s: deque(maxlen=600) for s in symbols}
        self.bars_5m: Dict[str, Deque[OhlcvBar]] = {s: deque(maxlen=200) for s in symbols}
        self._day_pv_sum: Dict[str, float] = {s: 0.0 for s in symbols}
        self._day_vol_sum: Dict[str, float] = {s: 0.0 for s in symbols}
        self.today_vol_series: Dict[str, List[float]] = {s: [] for s in symbols}
        self.cur_1m: Dict[str, Optional[OhlcvBar]] = {s: None for s in symbols}
        self.cur_5m: Dict[str, Optional[OhlcvBar]] = {s: None for s in symbols}
        self.ticks_1s: Dict[str, Deque[tuple[int, float]]] = {s: deque(maxlen=2000) for s in symbols}

    async def initialize(self) -> None:
        self._sfs = SFS1sReader()
        self.logger.info("market_fabric_initialized", symbols=self.symbols)

    async def ingest(self) -> None:
        if not self._sfs: return
        for sym in self.symbols:
            watermark_ms = self._last_seen_ts_ms.get(sym, 0)
            if not watermark_ms and self.bars_1m[sym]:
                watermark_ms = int(self.bars_1m[sym][-1].ts_end.timestamp() * 1000)
            rows = await self._sfs.last_n(sym, 300) 
            if rows:
                for r in reversed(rows): await self._process_row(sym, r)

    async def _process_row(self, symbol: str, row: Dict[str, Any]) -> None:
        ts_ms = int(row.get("ts", 0))
        last = self._last_seen_ts_ms.get(symbol, 0)
        if ts_ms <= last and (last - ts_ms) <= 120_000: return
        if not self._calibrated and symbol == "QQQ":
            local_now_ms = datetime.now().timestamp() * 1000
            self._time_offset_ms = local_now_ms - ts_ms; self._calibrated = True
        
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ET)
        # V1 Port: Strict RTH Boundary (L1365). No data after 16:00!
        if dt.time() < time(9, 30) or dt.time() > time(16, 0): return
        
        self._last_seen_ts_ms[symbol] = self._last_ingest_at_ms[symbol] = ts_ms
        price, vol = float(row.get("close", 0.0)), float(row.get("volume", 0.0))
        self.ticks_1s[symbol].append((ts_ms, price))
        self._update_ohlcv(self.bars_1m[symbol], self.cur_1m, symbol, price, vol, dt, 1)
        self._update_ohlcv(self.bars_5m[symbol], self.cur_5m, symbol, price, vol, dt, 5)

    def _update_ohlcv(self, buffer: Deque[OhlcvBar], current_map: Dict[str, Optional[OhlcvBar]], 
                     symbol: str, price: float, vol: float, dt: datetime, timeframe_min: int) -> None:
        m = (dt.minute // timeframe_min) * timeframe_min
        start = dt.replace(minute=m, second=0, microsecond=0)
        cur = current_map[symbol]
        if timeframe_min == 1 and vol > 0:
            self._day_pv_sum[symbol] += price * vol; self._day_vol_sum[symbol] += vol
        if cur is None or cur.ts_start != start:
            if cur is not None:
                buffer.append(cur)
                if timeframe_min == 1: self.today_vol_series[symbol].append(float(cur.volume))
            current_map[symbol] = OhlcvBar(ts_start=start, ts_end=start + timedelta(minutes=timeframe_min), open=price, high=price, low=price, close=price, volume=vol, pv_sum=price * vol)
        else:
            cur.high, cur.low, cur.close = max(cur.high, price), min(cur.low, price), price
            cur.volume += vol; cur.pv_sum += price * vol

    def consume_new_ticks(self, symbol: str) -> Iterable[tuple[int, float]]:
        buf = self.ticks_1s.get(symbol)
        if not buf: return
        last = self._logic_watermark_ms.get(symbol, 0)
        for t in list(buf):
            if t[0] > last: self._logic_watermark_ms[symbol] = t[0]; yield t

    def get_last_price(self, symbol: str) -> float:
        if symbol in self.cur_1m and self.cur_1m[symbol]: return self.cur_1m[symbol].close
        return 0.0

    def get_anchored_vwap(self, symbol: str) -> float:
        v = self._day_vol_sum.get(symbol, 0.0)
        return self._day_pv_sum.get(symbol, 0.0) / v if v > 1e-9 else 0.0

    def get_freshness_age(self, symbol: str, now: datetime) -> float:
        last_ms = self._last_ingest_at_ms.get(symbol, 0)
        if last_ms == 0: return 9999.0
        return (now.timestamp() * 1000 - self._time_offset_ms - last_ms) / 1000.0

    def get_v3m(self, symbol: str, at_ts_ms: Optional[int] = None) -> float:
        buf = self.ticks_1s.get(symbol)
        if not buf or len(buf) < 2: return 0.0
        now_ms = at_ts_ms if at_ts_ms is not None else buf[-1][0]
        target_ms = now_ms - 180_000
        p_now, p_ago = 0.0, 0.0
        for ts, px in reversed(list(buf)):
            if not p_now and ts <= now_ms: p_now = px
            if ts <= target_ms: p_ago = px; break
        return p_now - p_ago if (p_now and p_ago) else 0.0

    def has_continuous_window(self, symbol: str, window_sec: int = 180, gap_limit_sec: int = 5) -> bool:
        buf = self.ticks_1s.get(symbol)
        if not buf or len(buf) < 20: return False
        min_ts = buf[-1][0] - (window_sec * 1000); seg = [t for t in buf if t[0] >= min_ts]
        if len(seg) < 20: return False
        for i in range(1, len(seg)):
            if (seg[i][0] - seg[i-1][0]) / 1000.0 > gap_limit_sec: return False
        return True

    def check_health(self, now: datetime, timeout_sec: int = 120, miss_n: int = 6) -> None:
        if now.weekday() > 4 or not (time(9, 30, 15) <= now.time() <= time(16, 0)): return
        is_bad = False
        for sym in ["QQQ", "QQEW"]:
            if self.get_freshness_age(sym, now) > timeout_sec or self.get_last_price(sym) <= 0:
                is_bad = True; break
            if sym == "QQQ" and self.get_anchored_vwap(sym) <= 0: is_bad = True; break
        if is_bad:
            self.state["ff_miss_count"] = self.state.get("ff_miss_count", 0) + 1
            if self.state["ff_miss_count"] >= miss_n: raise RuntimeError("HEALTH_FAILURE")
        else: self.state["ff_miss_count"] = 0

    def reset(self) -> None:
        for sym in self.symbols:
            self.bars_1m[sym].clear(); self.bars_5m[sym].clear(); self.today_vol_series[sym].clear()
            self.cur_1m[sym] = self.cur_5m[sym] = None
            self.ticks_1s[sym].clear(); self._last_seen_ts_ms[sym] = self._logic_watermark_ms[sym] = 0
            self._day_pv_sum[sym] = self._day_vol_sum[sym] = 0.0
        self._calibrated = False; self._time_offset_ms = 0.0

    def get_last_n_bars(self, symbol: str, timeframe: str, n: int) -> List[OhlcvBar]:
        dq = self.bars_1m[symbol] if timeframe == "1m" else self.bars_5m[symbol]
        if not dq: return []
        start_idx = max(0, len(dq) - n)
        return list(islice(dq, start_idx, len(dq)))

    async def stop(self) -> None:
        if self._sfs: await self._sfs.close()

    def inject_historical_1m(self, symbol: str, bars: List[Any]) -> None:
        self._inject_bars(self.bars_1m.get(symbol), bars, timeframe_min=1)
        for b1 in bars:
            ts = b1.date if isinstance(b1.date, datetime) else datetime.combine(b1.date, time(0))
            self._update_ohlcv(self.bars_5m[symbol], self.cur_5m, symbol, float(b1.close), float(b1.volume), ts.replace(tzinfo=ET), 5)

    def inject_historical_5m(self, symbol: str, bars: List[Any]) -> None:
        self._inject_bars(self.bars_5m.get(symbol), bars, timeframe_min=5)

    def _inject_bars(self, buffer: Deque[OhlcvBar], bars: List[Any], timeframe_min: int) -> None:
        if buffer is None: return
        for b in bars:
            ts = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, time(0))
            ts = ts.replace(tzinfo=ET) if ts.tzinfo is None else ts
            if any(existing.ts_start == ts for existing in buffer): continue
            ohlcv = OhlcvBar(ts_start=ts, ts_end=ts + timedelta(minutes=timeframe_min), open=float(b.open), high=float(b.high), low=float(b.low), close=float(b.close), volume=float(b.volume), pv_sum=float(b.close * b.volume))
            buffer.append(ohlcv)
            if timeframe_min == 1:
                self.today_vol_series[symbol].append(float(ohlcv.volume))
                self._day_pv_sum[symbol] += ohlcv.pv_sum; self._day_vol_sum[symbol] += ohlcv.volume
