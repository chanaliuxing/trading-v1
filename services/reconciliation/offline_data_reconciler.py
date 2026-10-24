# services/reconciliation/offline_data_reconciler.py
"""
Offline Data Reconciler (P4)
- Runs nightly (recommended after 02:00 ET)
- Reads trading.bars_{res}_current, compares to theoretical RTH timeline
- Chunks gaps into <= max_range_minutes ranges, sends to Kafka request topic
- BackfillService(1m/5m/..) pulls from IBKR → sends to bar topic → CH Writer → ClickHouse

Usage examples:
  python -m services.reconciliation.offline_data_reconciler --res 1m --days 10 --symbols AAPL,MSFT
  python -m services.reconciliation.offline_data_reconciler --res 1m --start 2025-09-01 --end 2025-09-30
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time, timezone
from typing import Iterable, List, Tuple, Optional, Set

from aiokafka import AIOKafkaProducer
from zoneinfo import ZoneInfo

from libs.db.ch_pool import get_client
from libs.config import config
from libs.logger import get_logger

logger = get_logger(__name__)
NY_TZ = ZoneInfo(getattr(config, "timezone", "America/New_York"))

def ny_naive(dt: datetime) -> datetime:
    return dt.astimezone(NY_TZ).replace(tzinfo=None)

def parse_res_to_bar_size(res: str):
    s = res.strip().lower()
    if s in ("5s", "5 sec", "5 secs"): return ("5 secs", 5)
    if s in ("1m", "1 min"): return ("1 min", 60)
    if s in ("5m", "5 min"): return ("5 min", 300)
    if s in ("10m", "10 min"): return ("10 mins", 600)
    if s in ("15m", "15 min"): return ("15 mins", 900)
    if s in ("30m", "30 min"): return ("30 mins", 1800)
    if s in ("1h", "1 hour", "60m"): return ("1 hour", 3600)
    if s in ("2h", "2 hour", "2 hours"): return ("2 hours", 7200)
    if s in ("3h", "3 hour", "3 hours"): return ("3 hours", 10800)
    if s in ("4h", "4 hour", "4 hours"): return ("4 hours", 14400)
    if s in ("1d", "1 day", "1day"): return ("1 day", 86400)
    raise ValueError(f"Unsupported res: {res}")

def rth_bounds(day: date, is_daily: bool = False):
    """
    For intraday (1m, 5m, etc.): RTH 9:30 AM - 4:00 PM ET
    For daily (1d): Use 8:00 PM boundary (IBKR convention)
    """
    if is_daily:
        # Daily bars use 8 PM ET as the boundary (after market close)
        start_local = datetime.combine(day, time(20, 0), tzinfo=NY_TZ)
        end_local = start_local  # For daily, we check single timestamp per day
        return ny_naive(start_local), ny_naive(end_local)
    else:
        # Intraday: RTH hours
        start_local = datetime.combine(day, time(9, 30), tzinfo=NY_TZ)
        end_local = datetime.combine(day, time(16, 0), tzinfo=NY_TZ)
        return ny_naive(start_local), ny_naive(end_local)

def iter_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def chunk_ranges(missing_starts: List[datetime], step: timedelta, max_minutes: int):
    if not missing_starts: return []
    out = []
    cur_start = missing_starts[0]
    prev = cur_start
    limit = cur_start + timedelta(minutes=max_minutes)
    for ts in missing_starts[1:]:
        if (ts - prev) == step and ts < limit:
            prev = ts
            continue
        out.append((cur_start, prev + step))
        cur_start, prev, limit = ts, ts, ts + timedelta(minutes=max_minutes)
    out.append((cur_start, prev + step))
    return out

@dataclass
class ReconConfig:
    res: str = "1m"
    symbols: Optional[List[str]] = None     # None = auto-discover
    start: Optional[date] = None
    end: Optional[date] = None
    days: int = 3                           # If no start/end, go back N days
    request_topic: str = "md.backfill.request.1m.v1"
    use_rth: bool = True
    max_range_minutes: int = 120            # Max 2 hours per segment
    discover_limit: int = 500               # Auto-discover symbol limit

class OfflineDataReconciler:
    def __init__(self, cfg: ReconConfig):
        self.cfg = cfg
        self.db = get_client()  # Use global connection pool
        self.view = f"trading.bars_{self.cfg.res}_current"
        self.bar_size, self.sec = parse_res_to_bar_size(self.cfg.res)
        self.bootstrap = (getattr(config, "kafka_config", {}) or {}).get("bootstrap_servers")
        if not self.bootstrap:
            raise RuntimeError("Kafka bootstrap_servers not configured")
        self.producer: Optional[AIOKafkaProducer] = None

    async def __aenter__(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda v: v.encode("utf-8"),
            linger_ms=10,
        )
        await self.producer.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.producer:
            await self.producer.stop()
        # Note: ClickHouse client uses global connection pool - no close needed

    def _discover_symbols(self, day: date, limit: int) -> List[str]:
        # First try config universe (more reliable)
        universe = getattr(config, 'universe', None) or []
        if universe:
            logger.info("Using symbols from config universe", count=len(universe))
            return universe[:limit] if limit else universe

        # Fallback: query bars_5s table
        sql = """
        SELECT DISTINCT symbol
        FROM trading.bars_5s
        WHERE d = %(d)s
        LIMIT %(limit)s
        """
        try:
            rows = self.db.query(sql, {"d": day, "limit": limit})
            symbols = [r["symbol"] for r in rows]
            logger.info("Discovered symbols from bars_5s", count=len(symbols))
            return symbols
        except Exception as exc:
            logger.warning("symbol discovery failed", error=str(exc))
            return []

    def _existing_windows(self, symbol: str, start: datetime, end: datetime) -> Set[datetime]:
        sql = f"""
        SELECT ts_start
        FROM {self.view}
        WHERE symbol = %(symbol)s
          AND ts_start >= %(start)s
          AND ts_start <  %(end)s
        """
        rows = self.db.query(sql, {"symbol": symbol, "start": start, "end": end})
        # Convert to naive datetime for comparison (remove timezone if present)
        result = set()
        for r in rows:
            ts = r["ts_start"]
            if isinstance(ts, datetime) and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            result.add(ts)
        return result

    async def _enqueue(self, symbol: str, start_dt: datetime, end_dt: datetime) -> None:
        assert self.producer is not None
        payload = {
            "data": {
                "symbol": symbol,
                "from_ts": int(start_dt.replace(tzinfo=NY_TZ).timestamp()),
                "to_ts": int(end_dt.replace(tzinfo=NY_TZ).timestamp()),
                "use_rth": bool(self.cfg.use_rth),
                "bar_size": self.bar_size,
                "reason": "recon",
            }
        }
        await self.producer.send(self.cfg.request_topic, key=symbol, value=payload)
        logger.info("Recon enqueued", symbol=symbol, start=str(start_dt), end=str(end_dt))

    async def run(self) -> None:
        now_ny = datetime.now(NY_TZ)
        today = now_ny.date()
        is_daily = self.cfg.res in ("1d", "1 day", "1day")

        if self.cfg.start and self.cfg.end:
            start_day, end_day = self.cfg.start, self.cfg.end
        else:
            # Time-aware: If after 4 PM ET, include today's data for reconciliation
            market_close = datetime.strptime("16:00", "%H:%M").time()
            if now_ny.time() >= market_close:
                end_day = today
            else:
                end_day = today - timedelta(days=1)
            start_day = end_day - timedelta(days=max(1, int(self.cfg.days) - 1))

        logger.info(
            "Reconciliation date range",
            start=str(start_day),
            end=str(end_day),
            current_time_ny=now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
            includes_today=(end_day >= today),
            resolution=self.cfg.res,
            is_daily=is_daily,
        )

        # For daily bars, handle differently - check one timestamp per day
        if is_daily:
            symbols = self.cfg.symbols or self._discover_symbols(today, self.cfg.discover_limit)
            if not symbols:
                logger.warning("No symbols to reconcile for daily bars")
                return

            for day in iter_days(start_day, end_day):
                # Daily bars use 8 PM timestamp
                daily_ts, _ = rth_bounds(day, is_daily=True)

                for symbol in symbols:
                    # Check if this specific daily bar exists
                    have = self._existing_windows(symbol, daily_ts, daily_ts + timedelta(seconds=1))

                    if daily_ts not in have:
                        # Request the full day's data
                        # For daily bars, IBKR expects the date at market close (4 PM)
                        request_start = datetime.combine(day, time(16, 0), tzinfo=NY_TZ)
                        request_end = request_start + timedelta(days=1)
                        await self._enqueue(symbol, ny_naive(request_start), ny_naive(request_end))
        else:
            # Intraday bars - original logic
            step = timedelta(seconds=self.sec)
            for day in iter_days(start_day, end_day):
                rth_start, rth_end = rth_bounds(day, is_daily=False)
                symbols = self.cfg.symbols or self._discover_symbols(day, self.cfg.discover_limit)
                if not symbols:
                    logger.warning("No symbols to reconcile", day=str(day))
                    continue

                for symbol in symbols:
                    have = self._existing_windows(symbol, rth_start, rth_end)
                    missing = []
                    cur = rth_start
                    while cur < rth_end:
                        if cur not in have:
                            missing.append(cur)
                        cur += step
                    if not missing:
                        continue

                    shards = chunk_ranges(missing, step, self.cfg.max_range_minutes)
                    for (start_dt, end_dt) in shards:
                        await self._enqueue(symbol, start_dt, end_dt)

# CLI
def _parse_args() -> ReconConfig:
    p = argparse.ArgumentParser(description="Offline Data Reconciler (bars)")
    p.add_argument("--res", default="1m", help="resolution: 1m/5m/15m/30m/1h")
    p.add_argument("--symbols", default="", help="comma-separated symbols; default auto-discover")
    p.add_argument("--start", default="", help="start date YYYY-MM-DD")
    p.add_argument("--end", default="", help="end date YYYY-MM-DD")
    p.add_argument("--days", type=int, default=3, help="if no start/end, go back N calendar days")
    p.add_argument("--request-topic", default="", help="Kafka request topic; default md.backfill.request.{res}.v1")
    p.add_argument(
        '--use-rth',
        action='store_true',
        default=True,
        help="Include only RTH (Regular Trading Hours) data. Default is true."
    )
    p.add_argument("--max-range-minutes", type=int, default=120)
    p.add_argument("--discover-limit", type=int, default=500)
    args = p.parse_args()

    res = args.res
    req_topic = args.request_topic.strip() or f"md.backfill.request.{res}.v1"
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    return ReconConfig(
        res=res, symbols=syms, start=start, end=end, days=int(args.days),
        request_topic=req_topic, use_rth=bool(args.use_rth),
        max_range_minutes=int(args.max_range_minutes), discover_limit=int(args.discover_limit),
    )

async def _amain(cfg: ReconConfig): 
    async with OfflineDataReconciler(cfg) as recon:
        await recon.run()

def main(): 
    cfg = _parse_args(); asyncio.run(_amain(cfg))

if __name__ == "__main__":
    main()
