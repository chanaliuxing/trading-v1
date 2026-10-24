"""
Production providers for Decision Pipeline
Simple implementations that work with real data
"""
import asyncio
from typing import Any, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from libs.db.ch_pool import threadsafe_query
from libs.db.postgres import PostgresDB
from libs.dal.market_data_facade import MarketDataFacade
from libs.logger import get_logger

logger = get_logger(__name__)


class MarketStateProvider:
    """
    Provides market state (bid/ask/last/tick_size/halted)
    Uses MarketDataFacade for L1 quotes
    """

    def __init__(self, md: MarketDataFacade):
        self.md = md
        self._et = ZoneInfo("America/New_York")

    async def get(self, symbol: str) -> Dict[str, Any]:
        """
        Get market state for symbol

        Returns:
            Dict with bid_px, ask_px, mid_px, last_px, tick_size, halted, ssr_active
        """
        try:
            # Increased staleness tolerance to 120s to avoid blocking trades on short data gaps:
            # - Kafka producer batching delays
            # - ClickHouse insertion delays
            # - Short gaps in IB market data feed
            quote = await self.md.last_quote_async(symbol, staleness_sec=120)

            # CRITICAL FIX: MarketDataFacade returns keys 'bid' and 'ask', NOT 'bid_px' and 'ask_px'
            # This was the root cause of "Market data all zeros" warnings - data contract mismatch
            bid_px = quote.get('bid', 0.0)  # Changed from 'bid_px' to 'bid'
            ask_px = quote.get('ask', 0.0)  # Changed from 'ask_px' to 'ask'
            mid_px = (bid_px + ask_px) / 2 if bid_px and ask_px else 0.0
            quote_ts = quote.get("quote_ts")
            quote_age_sec = None
            try:
                if quote_ts is not None:
                    if isinstance(quote_ts, datetime):
                        qts = quote_ts if quote_ts.tzinfo is not None else quote_ts.replace(tzinfo=self._et)
                    else:
                        qts = datetime.fromisoformat(str(quote_ts))
                        if qts.tzinfo is None:
                            qts = qts.replace(tzinfo=self._et)
                    now_ts = datetime.now(qts.tzinfo)
                    quote_age_sec = max(0.0, (now_ts - qts).total_seconds())
            except Exception:
                quote_age_sec = None

            # Simple tick size estimation (TODO: use reference data)
            tick_size = 0.01  # Default for most stocks

            # MarketDataFacade doesn't return 'last_px', use mid_px as fallback
            # If we need actual last trade price, MarketDataFacade query needs to be extended
            last_px = mid_px

            result = {
                'symbol': symbol,
                'bid_px': bid_px,
                'ask_px': ask_px,
                'mid_px': mid_px,
                'last_px': last_px,
                'quote_ts': quote_ts,
                'quote_age_sec': quote_age_sec,
                'tick_size': tick_size,
                'halted': False,  # TODO: Check from market data
                'ssr_active': False  # TODO: Check from reference data
            }

            # Warn if all prices are zero (likely data gap)
            if bid_px == 0.0 and ask_px == 0.0 and last_px == 0.0:
                logger.warning("Market data all zeros within staleness window",
                             symbol=symbol,
                             staleness_sec=120,
                             raw_quote=quote)  # Changed to raw_quote to see actual return value

            return result

        except Exception as e:
            logger.error("Failed to get market state",
                        symbol=symbol,
                        error=str(e))
            # Return safe defaults
            return {
                'symbol': symbol,
                'bid_px': 0.0,
                'ask_px': 0.0,
                'mid_px': 0.0,
                'last_px': 0.0,
                'quote_ts': None,
                'quote_age_sec': None,
                'tick_size': 0.01,
                'halted': True,  # Conservative: assume halted if we can't get data
                'ssr_active': False
            }


class BarStore:
    """
    Provides historical bar access
    Uses ClickHouse bars_5s table
    """

    def __init__(self, ch):
        self.ch = ch  # ClickHouse client from global pool

    async def window(self, symbol: str, end_us: int, count: int) -> list:
        """
        Get historical bar window

        Args:
            symbol: Symbol
            end_us: End timestamp (microseconds)
            count: Number of bars to fetch

        Returns:
            List of bar dicts with ts_end_us, atr_bp, volume, quality
        """
        try:
            # Convert microseconds to datetime
            end_dt = datetime.fromtimestamp(end_us / 1e6)

            # JOIN bars_5s with features_5s to get ATR
            # atr_bp = (atr_14 / close) * 10000 converts absolute ATR to basis points
            # quality defaults to 'GOOD' if features exist, else 'UNKNOWN'
            sql = """
            SELECT
                toUnixTimestamp(b.ts_end) * 1000000 as ts_end_us,
                b.volume,
                if(f.atr_14 > 0 AND b.close > 0, f.atr_14 / b.close * 10000, 50.0) as atr_bp,
                if(f.atr_14 IS NOT NULL, 'GOOD', 'UNKNOWN') as quality
            FROM bars_5s b FINAL
            LEFT JOIN features_5s f FINAL
                ON b.symbol = f.symbol AND b.ts_end = f.ts
            WHERE b.symbol = {symbol:String}
              AND b.ts_end <= {end_dt:DateTime}
            ORDER BY b.ts_end DESC
            LIMIT {count:UInt32}
            """

            results = await asyncio.to_thread(
                threadsafe_query,
                self.ch,
                sql,
                {
                    'symbol': symbol,
                    'end_dt': end_dt,
                    'count': count,
                },
            )

            # Reverse to get chronological order
            return list(reversed(results))

        except Exception as e:
            logger.error("Failed to fetch bar window",
                        symbol=symbol,
                        end_us=end_us,
                        count=count,
                        error=str(e))
            return []


class FeatureStore:
    """
    Feature store (currently unused by validator)
    Placeholder for future feature access
    """
    pass


class ReserveLedger:
    """
    Capital reservation ledger
    Uses PostgreSQL to track reserved capital per strategy/symbol
    """

    def __init__(self, pg: PostgresDB):
        self.pg = pg

    async def snapshot(self) -> Dict[str, Any]:
        """
        Get current reservation snapshot

        Returns:
            Dict with 'items': {(strategy_id, symbol): {'qty': float, 'notional': float}}
        """
        try:
            # Primary source of truth: risk reservation ledger.
            # This must stay consistent with ReservationService stored procedures.
            rows = await self.pg.fetchall(
                """
                SELECT strategy_id, symbol, SUM(reserved_usd) AS reserved_usd
                FROM core.core_order_reservation
                WHERE state = 'HELD'
                GROUP BY strategy_id, symbol
                """
            )
            items = {}
            for row in rows or []:
                strategy_id = str(row.get("strategy_id") or "")
                symbol = str(row.get("symbol") or "")
                reserved = float(row.get("reserved_usd") or 0.0)
                key = (strategy_id, symbol)
                items[key] = {
                    "qty": 0.0,  # quantity is not authoritative in reservation ledger snapshot
                    "notional": reserved,
                    "amount_remaining": reserved,
                    "symbol": symbol,
                    "strategy_id": strategy_id,
                }

            return {'items': items}

        except Exception as e:
            logger.error("Failed to get reserve snapshot from reservation table", error=str(e))
            # Compatibility fallback for environments missing core_order_reservation.
            try:
                orders = await self.pg.fetchall(
                    """
                    SELECT strategy_id, symbol, SUM(qty) as open_qty
                    FROM core.core_order
                    WHERE state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                    GROUP BY strategy_id, symbol
                    """
                )
                items = {}
                for order in orders or []:
                    strategy_id = str(order.get("strategy_id") or "")
                    symbol = str(order.get("symbol") or "")
                    qty = float(order.get("open_qty") or 0.0)
                    # Fallback approximation only.
                    notional = abs(qty) * 100.0
                    items[(strategy_id, symbol)] = {
                        "qty": qty,
                        "notional": notional,
                        "amount_remaining": notional,
                        "symbol": symbol,
                        "strategy_id": strategy_id,
                    }
                logger.warning("Reserve ledger fallback in use (core_order approximation)")
                return {"items": items}
            except Exception as fallback_err:
                logger.error("Failed to get reserve snapshot fallback", error=str(fallback_err))
                return {'items': {}}


class ADVProvider:
    """
    Average daily volume provider
    Uses ClickHouse bars_1m_ibkr to calculate recent ADV
    """

    def __init__(self, ch):
        self.ch = ch  # ClickHouse client from global pool

    async def adv_usd(self, symbol: str, days: int = 20) -> float:
        """
        Calculate average daily volume in USD

        Args:
            symbol: Symbol
            days: Number of days to average (default 20)

        Returns:
            ADV in USD
        """
        try:
            sql = """
            SELECT AVG(daily_volume_usd) as adv_usd
            FROM (
                SELECT
                    toDate(ts_start) as date,
                    SUM(volume * close) as daily_volume_usd
                FROM bars_1m_ibkr FINAL
                WHERE symbol = {symbol:String}
                  AND ts_start >= now() - INTERVAL {days:UInt32} DAY
                GROUP BY date
            )
            """

            results = await asyncio.to_thread(
                threadsafe_query,
                self.ch,
                sql,
                {
                    'symbol': symbol,
                    'days': days,
                },
            )

            if results:
                adv = float(results[0]['adv_usd'])
                return adv if adv > 0 else 1_000_000.0  # Default $1M
            else:
                return 1_000_000.0  # Default

        except Exception as e:
            logger.error("Failed to calculate ADV",
                        symbol=symbol,
                        error=str(e))
            return 1_000_000.0  # Default


class TickSizeProvider:
    """
    Tick size provider
    Simple implementation (TODO: use reference data)
    """
    pass
