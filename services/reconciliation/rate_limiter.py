"""
Redis Token Bucket Rate Limiter
Based on doc/39.md specification

Implements shared token bucket across processes using Redis + Lua script.

Features:
- Atomic refill + consume via Lua script
- Configurable capacity and refill rate
- Multi-dimensional keys (global, per-strategy, per-symbol)
- Auto-expiring buckets to prevent stale state
"""
import asyncio
import time
from typing import Tuple, Optional

import redis.asyncio as redis

from libs.logger import get_logger

logger = get_logger(__name__)


class RedisTokenBucket:
    """
    Shared token bucket rate limiter using Redis

    Uses Lua script for atomic refill + consume operation.
    """

    # Lua script for atomic token bucket operation
    LUA_SCRIPT = """
    local key = KEYS[1]
    local cap = tonumber(ARGV[1])
    local rate = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local cost = tonumber(ARGV[4])

    local data = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(data[1]) or cap
    local ts = tonumber(data[2]) or now

    -- Refill tokens based on elapsed time
    if now > ts then
        local delta = (now - ts) / 1000.0
        tokens = math.min(cap, tokens + delta * rate)
        ts = now
    end

    -- Try to consume
    local ok = 0
    if tokens >= cost then
        tokens = tokens - cost
        ok = 1
    end

    -- Update state
    redis.call('HMSET', key, 'tokens', tokens, 'ts', ts)

    -- Set expiration (avoid stale buckets)
    local expire_ms = math.max(60000, math.floor(1000 * cap / math.max(rate, 0.001)))
    redis.call('PEXPIRE', key, expire_ms)

    return {ok, tokens}
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        namespace: str,
        capacity: int,
        refill_per_sec: float
    ):
        """
        Initialize token bucket

        Args:
            redis_client: Redis async client
            namespace: Namespace for keys (e.g., "qsys:rl:place")
            capacity: Maximum tokens (burst capacity)
            refill_per_sec: Tokens added per second
        """
        self.redis = redis_client
        self.namespace = namespace
        self.capacity = capacity
        self.refill_rate = refill_per_sec
        self.script_sha: Optional[str] = None

        logger.info(
            "TokenBucket initialized",
            namespace=namespace,
            capacity=capacity,
            refill_per_sec=refill_per_sec
        )

    async def _ensure_script(self) -> None:
        """Load Lua script into Redis if not already loaded"""
        if not self.script_sha:
            try:
                self.script_sha = await self.redis.script_load(self.LUA_SCRIPT)
                logger.debug("Lua script loaded", sha=self.script_sha)
            except Exception as e:
                logger.error("Failed to load Lua script", error=str(e))
                raise

    def _build_key(self, *parts: str) -> str:
        """
        Build Redis key from namespace and parts

        Args:
            *parts: Key parts (e.g., "global" or "strategy", "meanrev_v2")

        Returns:
            Redis key string
        """
        return ":".join([self.namespace, *[str(p) for p in parts]])

    async def try_acquire(
        self,
        key_suffix: Tuple[str, ...],
        cost: int = 1
    ) -> bool:
        """
        Non-blocking token acquisition

        Args:
            key_suffix: Key suffix tuple (e.g., ("global",) or ("strategy", "meanrev_v2"))
            cost: Number of tokens to consume

        Returns:
            True if tokens acquired successfully
        """
        await self._ensure_script()

        key = self._build_key(*key_suffix)
        now_ms = int(time.time() * 1000)

        try:
            # Execute Lua script
            result = await self.redis.evalsha(
                self.script_sha,
                1,  # Number of keys
                key,  # KEYS[1]
                self.capacity,  # ARGV[1]
                self.refill_rate,  # ARGV[2]
                now_ms,  # ARGV[3]
                cost  # ARGV[4]
            )

            ok, remaining = result
            success = ok == 1

            if not success:
                logger.debug(
                    "Rate limited",
                    key=key,
                    cost=cost,
                    remaining=remaining
                )

            return success

        except redis.ResponseError as e:
            # Script might have been evicted - reload
            logger.warning("Script error, reloading", error=str(e))
            self.script_sha = None
            return await self.try_acquire(key_suffix, cost)

    async def acquire(
        self,
        key_suffix: Tuple[str, ...],
        cost: int = 1,
        timeout_s: float = 1.0
    ) -> bool:
        """
        Blocking token acquisition with timeout

        Args:
            key_suffix: Key suffix tuple
            cost: Number of tokens to consume
            timeout_s: Maximum wait time in seconds

        Returns:
            True if tokens acquired within timeout
        """
        deadline = time.time() + timeout_s

        while time.time() < deadline:
            if await self.try_acquire(key_suffix, cost):
                return True

            # Sleep briefly before retry
            await asyncio.sleep(0.01)

        logger.warning(
            "Acquire timeout",
            key_suffix=key_suffix,
            cost=cost,
            timeout_s=timeout_s
        )

        return False

    async def peek(self, key_suffix: Tuple[str, ...]) -> float:
        """
        Peek at current token count without consuming

        Args:
            key_suffix: Key suffix tuple

        Returns:
            Current token count
        """
        key = self._build_key(*key_suffix)

        data = await self.redis.hmget(key, 'tokens', 'ts')

        if not data[0]:
            return float(self.capacity)

        tokens = float(data[0])
        ts = float(data[1] or 0)

        # Apply refill since last update
        now_ms = time.time() * 1000
        if now_ms > ts:
            delta_s = (now_ms - ts) / 1000.0
            tokens = min(self.capacity, tokens + delta_s * self.refill_rate)

        return tokens

    async def reset(self, key_suffix: Tuple[str, ...]) -> None:
        """
        Reset bucket to full capacity

        Args:
            key_suffix: Key suffix tuple
        """
        key = self._build_key(*key_suffix)
        await self.redis.delete(key)

        logger.info("Bucket reset", key=key)


class RateLimiterSet:
    """
    Set of rate limiters for different operations

    Manages place/modify/cancel rate limiters at different scopes.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize rate limiter set

        Args:
            redis_client: Redis async client
        """
        # Place order limiters
        self.place_global = RedisTokenBucket(
            redis_client,
            namespace="qsys:rl:place",
            capacity=15,
            refill_per_sec=15.0  # 15 orders/sec globally
        )

        # Modify order limiters
        self.modify_global = RedisTokenBucket(
            redis_client,
            namespace="qsys:rl:modify",
            capacity=20,
            refill_per_sec=20.0  # 20 modifies/sec globally
        )

        # Cancel order limiters
        self.cancel_global = RedisTokenBucket(
            redis_client,
            namespace="qsys:rl:cancel",
            capacity=20,
            refill_per_sec=20.0  # 20 cancels/sec globally
        )

        logger.info("RateLimiterSet initialized")

    async def check_place(
        self,
        strategy_id: str,
        symbol: str,
        timeout_s: float = 1.0
    ) -> bool:
        """
        Check place rate limits at all scopes

        Args:
            strategy_id: Strategy ID
            symbol: Symbol
            timeout_s: Timeout for acquiring tokens

        Returns:
            True if all rate limits passed
        """
        # Check global, strategy, and symbol limits
        global_ok = await self.place_global.acquire(("global",), timeout_s=timeout_s)
        if not global_ok:
            return False

        strategy_ok = await self.place_global.acquire(
            ("strategy", strategy_id),
            timeout_s=timeout_s
        )
        if not strategy_ok:
            return False

        symbol_ok = await self.place_global.acquire(
            ("symbol", symbol),
            timeout_s=timeout_s
        )

        return symbol_ok

    async def check_modify(
        self,
        strategy_id: str,
        symbol: str,
        timeout_s: float = 1.0
    ) -> bool:
        """
        Check modify rate limits at all scopes

        Args:
            strategy_id: Strategy ID
            symbol: Symbol
            timeout_s: Timeout for acquiring tokens

        Returns:
            True if all rate limits passed
        """
        global_ok = await self.modify_global.acquire(("global",), timeout_s=timeout_s)
        if not global_ok:
            return False

        strategy_ok = await self.modify_global.acquire(
            ("strategy", strategy_id),
            timeout_s=timeout_s
        )

        return strategy_ok

    async def check_cancel(
        self,
        strategy_id: str,
        symbol: str,
        timeout_s: float = 1.0
    ) -> bool:
        """
        Check cancel rate limits at all scopes

        Args:
            strategy_id: Strategy ID
            symbol: Symbol
            timeout_s: Timeout for acquiring tokens

        Returns:
            True if all rate limits passed
        """
        global_ok = await self.cancel_global.acquire(("global",), timeout_s=timeout_s)
        if not global_ok:
            return False

        strategy_ok = await self.cancel_global.acquire(
            ("strategy", strategy_id),
            timeout_s=timeout_s
        )

        return strategy_ok
