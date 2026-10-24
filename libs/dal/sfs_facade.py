"""
SFS 1-second Redis Facade

Lightweight reader for 1s bars stored in Redis ring buffers by sfs_1s_service.

Key format: sfs:1s:<symbol>
Type: LIST, newest at index 0 (LPUSH)
Entry: compact JSON string {ts, open, high, low, close, volume, trades}
"""
from __future__ import annotations

import json
from typing import List, Dict, Any, Optional
import redis.asyncio as aioredis

from libs.logger import get_logger
from libs.config import config
from strategies.shared_data import shared_underlying_data_plane

logger = get_logger(__name__)


class SFS1sReader:
    def __init__(self, redis_url: Optional[str] = None, namespace: str = "sfs:1s") -> None:
        self.redis = aioredis.from_url(redis_url or config.redis_config, decode_responses=True)
        self.ns = namespace
        cfg = config.get("services.sfs_1s", {}) or {}
        self._shared_plane_enabled = bool(cfg.get("shared_underlying_plane_read_enabled", True))
        self._shared_plane_namespace = str(cfg.get("shared_underlying_namespace", "global") or "global").strip().lower()

    def _key(self, symbol: str) -> str:
        return f"{self.ns}:{symbol}"

    def _plane_rows(self, symbol: str, n: int) -> List[Dict[str, Any]]:
        if not self._shared_plane_enabled:
            return []
        try:
            rows = shared_underlying_data_plane().recent_bars(
                symbol=symbol,
                namespace=self._shared_plane_namespace,
                limit=max(1, int(n)),
            )
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts_ms = int(float(row.get("ts_epoch_s") or 0.0) * 1000.0)
            except Exception:
                ts_ms = 0
            if ts_ms <= 0:
                continue
            out.append(
                {
                    "ts": ts_ms,
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "vwap": row.get("vwap"),
                }
            )
        return list(reversed(out))

    @staticmethod
    def _parse(row: str) -> Dict[str, Any]:
        try:
            return json.loads(row)
        except Exception:  # noqa: BLE001
            return {}

    async def last(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the most recent 1s bar for symbol (or None)"""
        try:
            v = await self.redis.lindex(self._key(symbol), 0)
            if v:
                return self._parse(v)
            rows = self._plane_rows(symbol, 1)
            return rows[0] if rows else None
        except Exception as e:  # noqa: BLE001
            logger.error("SFS1sReader.last failed", error=str(e), symbol=symbol)
            rows = self._plane_rows(symbol, 1)
            return rows[0] if rows else None

    async def last_n(self, symbol: str, n: int = 60) -> List[Dict[str, Any]]:
        """Get up to N most-recent 1s bars (newest..older)"""
        n = max(1, int(n))
        try:
            vals = await self.redis.lrange(self._key(symbol), 0, n - 1)
            rows = [self._parse(v) for v in vals]
            return rows or self._plane_rows(symbol, n)
        except Exception as e:  # noqa: BLE001
            logger.error("SFS1sReader.last_n failed", error=str(e), symbol=symbol)
            return self._plane_rows(symbol, n)

    async def close(self) -> None:
        try:
            await self.redis.close()
        except Exception:
            pass
