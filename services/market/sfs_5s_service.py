"""
SFS-5s Service: The L2 (Layer 2) Sliding Feature Service.

Responsibilities:
- Consumes 1s microbars from Redis (produced by an L1 service like sfs_1s_service).
- Computes 5-second sliding window features at a 1-second stride.
- Persists the calculated features to ClickHouse for strategies and offline analysis.
- Exports Prometheus metrics for monitoring.
"""
import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

import redis.asyncio as aioredis

from libs.config import config
from libs.logger import get_logger
# Assuming a shared ClickHouse client pool exists
from libs.db.ch_pool import get_client as get_ch_client

logger = get_logger(__name__)

class SFS5sService:
    """
    Calculates 5s sliding window features based on 1s microbars from Redis
    and persists them to ClickHouse.
    """
    def __init__(
        self,
        redis_url: Optional[str] = None,
        ch_client = None,
        symbols: Optional[List[str]] = None,
        config_group: str = "sfs_5s",
    ) -> None:
        """
        Initializes the SFS-5s service.

        Args:
            redis_url: URL for the Redis instance containing 1s microbars.
            ch_client: An instance of a ClickHouse client.
            symbols: A list of symbols to process. If None, uses the global config universe.
            config_group: The key for this service's configuration in the main config file.
        """
        self.redis = aioredis.from_url(redis_url or config.redis_config, decode_responses=True)
        self.ch = ch_client or get_ch_client()
        
        service_cfg = config.get('services', {}).get(config_group, {})
        self.flush_period_sec: float = service_cfg.get("flush_period_ms", 1000) / 1000.0
        self.lookback_n: int = service_cfg.get("lookback_n", 5)
        self.symbols: List[str] = symbols or config.get("universe", [])
        self.ch_table_name: str = service_cfg.get("ch_table", "trading.sfs_features")

        self._running = False
        logger.info(f"SFS5sService initialized for {len(self.symbols)} symbols.")

    def _get_redis_key(self, symbol: str) -> str:
        """Constructs the Redis key for the 1s microbar list."""
        # This assumes the L1 service uses this key format
        return f"sfs:1s:{symbol}"

    async def _fetch_window_from_redis(self, symbol: str) -> List[dict]:
        """Fetches the latest N microbars for a symbol from Redis."""
        try:
            key = self._get_redis_key(symbol)
            # LRANGE 0 to N-1 gets the latest N items if using LPUSH
            raw_items = await self.redis.lrange(key, 0, self.lookback_n - 1)
            return [json.loads(item) for item in raw_items if item]
        except Exception as e:
            logger.error(f"SFS-5s: Failed to fetch window from Redis for {symbol}", error=str(e))
            return []

    def _calculate_sliding_feature(self, symbol: str, microbars: List[dict]) -> Optional[dict]:
        """
        Core logic to calculate one 5s sliding feature row from 5 1s microbars.
        This is the primary target for unit testing.
        """
        if len(microbars) < self.lookback_n:
            return None

        # The items from Redis (LPUSH) are newest to oldest. Reverse for chronological order.
        window = list(reversed(microbars))

        try:
            # TODO: Implement the full feature calculation logic as defined in the plan.
            # This is a placeholder implementation.
            open_px = float(window[0].get("open", 0.0))
            close_px = float(window[-1].get("close", 0.0))
            high_px = max(float(b.get("high", 0.0)) for b in window)
            low_px = min(float(b.get("low", 0.0)) for b in window)
            total_volume = sum(float(b.get("volume", 0.0)) for b in window)
            
            # Placeholder for other features
            vwap_1m_tumble = 0.0 # This should come from a 1m accumulator
            atr_1m_tumble = 0.0  # This should come from a 1m accumulator

            ts_ny = datetime.fromtimestamp(int(window[-1]["ts"]) / 1000.0, tz=ZoneInfo("America/New_York"))

            return {
                "symbol": symbol,
                "gran": '5s',
                "ts": ts_ny,
                "low": low_px,
                "high": high_px,
                "close": close_px,
                "volume": total_volume,
                "vwap_1m_tumble": vwap_1m_tumble,
                "atr_1m_tumble": atr_1m_tumble,
                # ... add all other fields from DDL, possibly as None/0.0 for now
            }
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"SFS-5s: Failed to calculate feature for {symbol}", error=str(e), window_data=window)
            return None

    async def run(self) -> None:
        """Main service loop."""
        self._running = True
        logger.info("SFS-5s service main loop started.")
        while self._running:
            start_time = time.time()
            
            feature_rows = []
            for symbol in self.symbols:
                # Fetch the latest data for each symbol
                microbars = await self._fetch_window_from_redis(symbol)
                
                # Calculate the feature if data is sufficient
                feature_row = self._calculate_sliding_feature(symbol, microbars)
                if feature_row:
                    feature_rows.append(feature_row)
            
            # Batch insert into ClickHouse
            if feature_rows:
                try:
                    # TODO: Implement the actual batch insert logic using the ch_client
                    # self.ch.insert(self.ch_table_name, feature_rows, column_names=list(feature_rows[0].keys()))
                    logger.debug(f"SFS-5s: Would insert {len(feature_rows)} rows into ClickHouse.")
                    pass # Placeholder
                except Exception as e:
                    logger.error("SFS-5s: ClickHouse insert failed", error=str(e))

            # Maintain the loop cadence (e.g., 1 second)
            elapsed_sec = time.time() - start_time
            sleep_duration = max(0, self.flush_period_sec - elapsed_sec)
            await asyncio.sleep(sleep_duration)

    async def stop(self) -> None:
        """Gracefully stops the service."""
        self._running = False
        logger.info("SFS-5s service stopping.")
        # Optional: flush any remaining buffer before exiting
        await self.redis.close()
