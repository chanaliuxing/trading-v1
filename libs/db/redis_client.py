"""
Redis client for state bus and caching
"""
import redis
import json
from typing import Any, Optional
from ..config import config
from ..logger import get_logger

logger = get_logger(__name__)


class RedisClient:
    """Redis client wrapper"""

    def __init__(self):
        """Initialize Redis client"""
        redis_url = config.redis_config
        self.client = redis.from_url(redis_url, decode_responses=True)
        logger.info("Redis client initialized", url=redis_url)

    def get(self, key: str) -> Optional[str]:
        """
        Get value by key

        Args:
            key: Key name

        Returns:
            Value or None
        """
        return self.client.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        """
        Set key-value pair

        Args:
            key: Key name
            value: Value
            ex: Expiration time in seconds (optional)
        """
        self.client.set(key, value, ex=ex)

    def get_json(self, key: str) -> Optional[Any]:
        """
        Get JSON value by key

        Args:
            key: Key name

        Returns:
            Deserialized JSON or None
        """
        value = self.get(key)
        if value:
            return json.loads(value)
        return None

    def set_json(self, key: str, value: Any, ex: Optional[int] = None) -> None:
        """
        Set JSON value

        Args:
            key: Key name
            value: Value (will be JSON-serialized)
            ex: Expiration time in seconds (optional)
        """
        self.set(key, json.dumps(value), ex=ex)

    def delete(self, *keys: str) -> int:
        """
        Delete keys

        Args:
            keys: Key names

        Returns:
            Number of keys deleted
        """
        return self.client.delete(*keys)

    def exists(self, key: str) -> bool:
        """
        Check if key exists

        Args:
            key: Key name

        Returns:
            True if exists, False otherwise
        """
        return self.client.exists(key) > 0

    def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on key

        Args:
            key: Key name
            seconds: Expiration time in seconds

        Returns:
            True if successful, False otherwise
        """
        return self.client.expire(key, seconds)

    def hget(self, name: str, key: str) -> Optional[str]:
        """
        Get hash field value

        Args:
            name: Hash name
            key: Field key

        Returns:
            Field value or None
        """
        return self.client.hget(name, key)

    def hset(self, name: str, key: str, value: str) -> int:
        """
        Set hash field value

        Args:
            name: Hash name
            key: Field key
            value: Field value

        Returns:
            Number of fields added
        """
        return self.client.hset(name, key, value)

    def hgetall(self, name: str) -> dict:
        """
        Get all hash fields

        Args:
            name: Hash name

        Returns:
            Dictionary of all fields
        """
        return self.client.hgetall(name)

    def hdel(self, name: str, *keys: str) -> int:
        """
        Delete one or more hash fields.

        Args:
            name: Hash name
            keys: Field names

        Returns:
            Number of fields deleted
        """
        if not keys:
            return 0
        return self.client.hdel(name, *keys)

    def publish(self, channel: str, message: str) -> int:
        """
        Publish message to channel

        Args:
            channel: Channel name
            message: Message to publish

        Returns:
            Number of subscribers that received the message
        """
        return self.client.publish(channel, message)

    def health_check(self) -> bool:
        """
        Check Redis health

        Returns:
            True if healthy, False otherwise
        """
        try:
            return self.client.ping()
        except Exception as e:
            logger.error("Redis health check failed", error=str(e))
            return False

    def close(self):
        """Close the client connection"""
        self.client.close()
        logger.info("Redis client closed")
