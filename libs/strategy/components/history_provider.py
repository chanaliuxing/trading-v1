"""
HistoryProvider Component
Handles fallback data recovery with 30s network timeouts (V1 Port).
"""
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from .base import StrategyComponent

ET = ZoneInfo("America/New_York")

class HistoryProvider(StrategyComponent):
    async def initialize(self) -> None: pass

    async def fetch_ibkr_historical_bars(self, symbol: str, duration_str: str, 
                                       bar_size: str, use_rth: bool = True) -> tuple[List[Any], str, int]:
        ib = self._get_ib_instance()
        if not ib: return [], "ibkr_disconnected", 0
        try:
            from ib_insync import Stock
            contract = Stock(symbol, "SMART", "USD")
            qualified = await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=10.0)
            if qualified: contract = qualified[0]
            
            # V1 Port: Explicit 30s timeout for network IO
            bars = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(
                    contract, endDateTime="", durationStr=duration_str,
                    barSizeSetting=bar_size, whatToShow="TRADES", useRTH=use_rth
                ), timeout=30.0
            )
            return bars or [], "ibkr_history", len(bars) if bars else 0
        except Exception as e:
            self.logger.error("ibkr_fetch_failed", symbol=symbol, error=str(e))
            return [], "error", 0

    async def fetch_ibkr_1m_bars(self, symbol: str, start: datetime, end: datetime) -> tuple[List[Any], str, int]:
        duration_sec = (end - start).total_seconds()
        return await self.fetch_ibkr_historical_bars(symbol, f"{int(duration_sec + 60)} S", "1 min", True)

    async def fetch_local_db_1m_bars(self, symbol: str, start: datetime, end: datetime) -> tuple[List[Any], str, int]:
        return await self._fetch_local_db_bars(symbol, start, end, "1 min")

    async def fetch_local_db_5m_bars(self, symbol: str, start: datetime, end: datetime) -> tuple[List[Any], str, int]:
        return await self._fetch_local_db_bars(symbol, start, end, "5 mins")

    async def _fetch_local_db_bars(self, symbol: str, start: datetime, end: datetime, interval: str) -> tuple[List[Any], str, int]:
        try:
            from libs.db.ch_pool import get_client
            client = get_client()
            ch_func = "toStartOfMinute" if interval == "1 min" else "toStartOfFiveMinutes"
            query = (
                f"SELECT {ch_func}(ts) as bar_start, argMin(price, ts), max(price), "
                "min(price), argMax(price, ts), sum(qty) "
                "FROM core.lake_trades "
                "WHERE symbol = {symbol:String} "
                "AND ts >= {start:DateTime} "
                "AND ts <= {end:DateTime} "
                "GROUP BY bar_start ORDER BY bar_start ASC"
            )
            params = {
                "symbol": str(symbol or "").strip().upper(),
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            }
            res = await asyncio.to_thread(lambda: client.execute(query, parameters=params))
            if not res: return [], "clickhouse_empty", 0
            from dataclasses import make_dataclass
            Bar = make_dataclass("Bar", ["date", "open", "high", "low", "close", "volume"])
            return [Bar(r[0], r[1], r[2], r[3], r[4], r[5]) for r in res], "ch_reconstructed", len(res)
        except Exception as e:
            self.logger.warning("clickhouse_history_fetch_failed", symbol=symbol, interval=interval, error=str(e))
            self._mark_data_unavailable(
                source="clickhouse",
                symbol=symbol,
                interval=interval,
                reason="clickhouse_history_fetch_failed",
                error=str(e),
            )
            return [], "ch_error", 0

    def _mark_data_unavailable(self, *, source: str, symbol: str, interval: str, reason: str, error: str) -> None:
        payload = {
            "dataset": "historical_bars",
            "source": source,
            "symbol": str(symbol or "").strip().upper(),
            "interval": interval,
            "reason": reason,
            "error": error,
        }
        try:
            self.state["data_unavailable_gate"] = payload
            self.state["data_integrity_warning"] = True
        except Exception:
            pass
        try:
            reporter = getattr(self.ctx, "report_data_unavailable", None)
            if callable(reporter):
                reporter(payload)
        except Exception:
            pass

    async def fetch_ibkr_price_base(self, symbol: str, now: datetime) -> tuple[Optional[float], str, int]:
        ib = self._get_ib_instance()
        if not ib: return None, "not_connected", 0
        try:
            from ib_insync import Stock
            contract = Stock(symbol, "SMART", "USD")
            window_end = now.replace(hour=9, minute=30, second=10, microsecond=0)
            bars = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(contract, endDateTime=window_end, durationStr="30 S", barSizeSetting="1 secs", whatToShow="TRADES", useRTH=True),
                timeout=30.0
            )
            if bars:
                pv, vv = 0.0, 0.0
                for b in bars:
                    dt = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, time(0))
                    if time(9,30) <= dt.time() < time(9,30,10): pv += b.close * b.volume; vv += b.volume
                if vv > 0: return pv / vv, "ibkr_1s", 1
        except Exception: pass
        return None, "no_data", 0

    def _get_ib_instance(self):
        try:
            ib = self.ctx.engine.market_data_service.ib_adapter.ib
            if ib and ib.isConnected(): return ib
        except Exception: pass
        return None
