"""
HashiCorp Vault Client

Manages secrets retrieval from Vault using AppRole authentication.
Secrets are cached and auto-rotated based on token TTL.
"""
import os
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass
import asyncio

import hvac
from hvac.exceptions import VaultError

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class VaultConfig:
    """Vault configuration"""
    url: str
    role_id: str
    secret_id: str
    mount_point: str = "trading"
    cache_ttl_s: int = 3600  # 1 hour


class VaultClient:
    """
    HashiCorp Vault client for secrets management

    Uses AppRole authentication and in-memory caching.
    """

    def __init__(self, config: VaultConfig):
        """
        Initialize Vault client

        Args:
            config: Vault configuration
        """
        self.config = config
        self.client = hvac.Client(url=config.url)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: Dict[str, float] = {}
        self._token_expiry: Optional[float] = None

    async def authenticate(self):
        """Authenticate with Vault using AppRole"""
        try:
            response = self.client.auth.approle.login(
                role_id=self.config.role_id,
                secret_id=self.config.secret_id
            )

            # Set token
            self.client.token = response["auth"]["client_token"]
            self._token_expiry = time.time() + response["auth"]["lease_duration"]

            logger.info(
                "Vault authentication successful",
                ttl=response["auth"]["lease_duration"]
            )

        except VaultError as e:
            logger.error("Vault authentication failed", error=str(e))
            raise

    async def get_secret(self, path: str, key: Optional[str] = None) -> Any:
        """
        Get secret from Vault

        Args:
            path: Secret path (e.g., "postgres", "ib")
            key: Optional specific key to retrieve

        Returns:
            Secret value(s)
        """
        # Check authentication
        if not self.client.token or self._is_token_expired():
            await self.authenticate()

        # Check cache
        cache_key = f"{self.config.mount_point}/{path}"
        if cache_key in self._cache:
            if time.time() - self._cache_ts[cache_key] < self.config.cache_ttl_s:
                logger.debug("Vault cache hit", path=path)
                data = self._cache[cache_key]
                return data.get(key) if key else data

        # Fetch from Vault
        try:
            response = self.client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point=self.config.mount_point
            )

            data = response["data"]["data"]

            # Update cache
            self._cache[cache_key] = data
            self._cache_ts[cache_key] = time.time()

            logger.info("Vault secret retrieved", path=path, cached=True)

            return data.get(key) if key else data

        except VaultError as e:
            logger.error("Failed to retrieve secret", path=path, error=str(e))
            raise

    async def get_postgres_config(self) -> Dict[str, Any]:
        """Get PostgreSQL configuration"""
        return await self.get_secret("postgres")

    async def get_clickhouse_config(self) -> Dict[str, Any]:
        """Get ClickHouse configuration"""
        return await self.get_secret("clickhouse")

    async def get_redis_config(self) -> Dict[str, Any]:
        """Get Redis configuration"""
        return await self.get_secret("redis")

    async def get_kafka_config(self) -> Dict[str, Any]:
        """Get Kafka configuration"""
        return await self.get_secret("kafka")

    async def get_ib_credentials(self) -> Dict[str, Any]:
        """Get IB credentials"""
        return await self.get_secret("ib")

    async def get_monitoring_config(self) -> Dict[str, Any]:
        """Get monitoring configuration"""
        return await self.get_secret("monitoring")

    async def get_api_keys(self) -> Dict[str, Any]:
        """Get external API keys"""
        return await self.get_secret("api")

    def _is_token_expired(self) -> bool:
        """Check if token is expired"""
        if not self._token_expiry:
            return True

        # Renew 5 minutes before expiry
        return time.time() >= (self._token_expiry - 300)

    async def renew_token(self):
        """Renew Vault token"""
        try:
            response = self.client.auth.token.renew_self()
            self._token_expiry = time.time() + response["auth"]["lease_duration"]
            logger.info("Vault token renewed", ttl=response["auth"]["lease_duration"])

        except VaultError as e:
            logger.warning("Token renewal failed, re-authenticating", error=str(e))
            await self.authenticate()

    async def start_token_renewal_task(self):
        """Start background token renewal task"""
        while True:
            try:
                # Check every 5 minutes
                await asyncio.sleep(300)

                if self._is_token_expired():
                    await self.renew_token()

            except Exception as e:
                logger.error("Token renewal task error", error=str(e))


def get_vault_client() -> VaultClient:
    """
    Get configured Vault client

    Reads configuration from environment variables:
    - VAULT_ADDR: Vault URL
    - VAULT_ROLE_ID: AppRole role ID
    - VAULT_SECRET_ID: AppRole secret ID
    """
    config = VaultConfig(
        url=os.getenv("VAULT_ADDR", "http://localhost:8200"),
        role_id=os.getenv("VAULT_ROLE_ID", ""),
        secret_id=os.getenv("VAULT_SECRET_ID", ""),
        mount_point=os.getenv("VAULT_MOUNT_POINT", "trading"),
        cache_ttl_s=int(os.getenv("VAULT_CACHE_TTL", "3600"))
    )

    if not config.role_id or not config.secret_id:
        raise ValueError("VAULT_ROLE_ID and VAULT_SECRET_ID must be set")

    return VaultClient(config)


# Example usage in services
async def example_usage():
    """Example: Using Vault client in a service"""
    vault = get_vault_client()
    await vault.authenticate()

    # Start token renewal task
    asyncio.create_task(vault.start_token_renewal_task())

    # Get secrets
    pg_config = await vault.get_postgres_config()
    ib_creds = await vault.get_ib_credentials()

    # Use secrets
    import asyncpg
    conn = await asyncpg.connect(
        host=pg_config["host"],
        port=int(pg_config["port"]),
        user=pg_config["username"],
        password=pg_config["password"],
        database=pg_config["database"]
    )
