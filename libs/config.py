"""
Configuration management module
Loads config from config.yml and .env
"""
import os
import yaml
from pathlib import Path
from typing import Dict, Any, List
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def _normalize_kafka_bootstrap_servers(raw: str | List[str] | None) -> List[str]:
    servers: List[str] = []
    if raw is None:
        return ["127.0.0.1:9092"]
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = raw
    for item in items:
        s = str(item or "").strip().strip('"').strip("'")
        if not s:
            continue
        if s.startswith("localhost:"):
            s = "127.0.0.1:" + s.split(":", 1)[1]
        servers.append(s)
    return servers or ["127.0.0.1:9092"]

class Config:
    """Configuration singleton"""

    _instance = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        """Load configuration from config.yml"""
        # First try config.yml, fallback to config.example.yml
        config_path = Path('config.yml')
        if not config_path.exists():
            config_path = Path('config.example.yml')

        if config_path.exists():
            with open(config_path, 'r') as f:
                self._config = yaml.safe_load(f)
        else:
            raise FileNotFoundError("No config file found (config.yml or config.example.yml)")

    def get(self, key: str, default=None):
        """Get config value by dot notation key"""
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default

    @property
    def pg_config(self) -> Dict[str, Any]:
        """PostgreSQL configuration"""
        return {
            'host': os.getenv('PG_HOST', 'localhost'),
            'port': int(os.getenv('PG_PORT', 5432)),
            'database': os.getenv('PG_DB', 'test'),
            'user': os.getenv('PG_USER', 'postgres'),
            'password': os.getenv('PG_PASSWORD', 'postgres')
        }

    @property
    def clickhouse_config(self) -> Dict[str, Any]:
        """ClickHouse configuration"""
        return {
            'host': os.getenv('CH_HOST', 'localhost'),
            'port': int(os.getenv('CH_PORT', 8123)),
            'database': os.getenv('CH_DB', 'trading'),
            'user': os.getenv('CH_USER', 'default'),
            'password': os.getenv('CH_PASSWORD', ''),
            'timezone': os.getenv('CH_TIMEZONE', self.get('clickhouse.timezone', 'UTC'))
        }

    @property
    def redis_config(self) -> str:
        """Redis configuration URL"""
        return os.getenv('STATEBUS_REDIS', 'redis://127.0.0.1:6379/0')

    @property
    def kafka_config(self) -> Dict[str, Any]:
        """Kafka configuration"""
        kafka_servers = os.getenv('KAFKA_BOOTSTRAP_SERVERS', self.get('kafka.bootstrap_servers', '127.0.0.1:9092'))
        return {
            'bootstrap_servers': _normalize_kafka_bootstrap_servers(kafka_servers),
            'acks': self.get('kafka.acks', 'all'),
            'enable_idempotence': self.get('kafka.enable_idempotence', True),
            'compression_type': self.get('kafka.compression_type', 'lz4'),
            'linger_ms': self.get('kafka.linger_ms', 5),
            'batch_size': self.get('kafka.batch_size', 131072)
        }

    @property
    def ib_config(self) -> Dict[str, Any]:
        """Interactive Brokers configuration"""
        mode = os.getenv('TRADING_MODE', self.get('mode', 'paper'))
        port = int(os.getenv('IB_PAPER_PORT' if mode == 'paper' else 'IB_LIVE_PORT', self.get('ib.port', 4002)))

        return {
            'host': os.getenv('IB_HOST', self.get('ib.host', '127.0.0.1')),
            'port': port,
            'client_id': int(os.getenv('IB_CLIENT_ID', self.get('ib.client_id', 7))),
            'mode': mode
        }

    @property
    def universe(self) -> list:
        """Trading universe symbols"""
        return self._config.get('universe', [])

    @property
    def all(self) -> Dict[str, Any]:
        """Get all configuration"""
        return self._config


# Global config instance
config = Config()
