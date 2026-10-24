"""
Configuration loader for EnhancedModelSignalStrategy with hot-reload support.

This module provides a centralized way to load per-symbol configurations from JSON files,
avoiding config.yml bloat and enabling hot-reload during runtime.

Architecture:
    - Per-symbol config files stored in: strategies/configs/enhanced_ml_{symbol}.json
    - File-based hot-reload with mtime tracking (no system modifications)
    - Thread-safe caching with TTL
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)


class ConfigLoader:
    """
    Hot-reloadable configuration loader for per-symbol strategy configs.

    Features:
        - File-based hot-reload (checks mtime)
        - Thread-safe caching
        - Fallback to defaults if file missing
        - Merge with base config
    """

    def __init__(
        self,
        config_dir: str = "strategies/configs",
        prefix: str = "enhanced_ml",
        check_interval_sec: float = 60.0,
    ):
        """
        Initialize config loader.

        Args:
            config_dir: Directory containing config files
            prefix: File name prefix (e.g., "enhanced_ml" -> "enhanced_ml_AAPL.json")
            check_interval_sec: Minimum time between mtime checks (default: 60s)
        """
        self.config_dir = Path(config_dir)
        self.prefix = prefix
        self.check_interval = timedelta(seconds=check_interval_sec)

        # Thread-safe cache: {symbol: (config_dict, mtime, last_check_time)}
        self._cache: Dict[str, tuple[Dict[str, Any], float, datetime]] = {}
        self._lock = RLock()

        logger.info(
            "ConfigLoader initialized",
            config_dir=str(self.config_dir),
            prefix=prefix,
            check_interval_sec=check_interval_sec,
        )

    def _get_config_path(self, symbol: str) -> Path:
        """Get config file path for symbol."""
        filename = f"{self.prefix}_{symbol.upper()}.json"
        return self.config_dir / filename

    def _should_reload(self, symbol: str, file_path: Path) -> bool:
        """Check if config should be reloaded based on mtime and cache TTL."""
        with self._lock:
            if symbol not in self._cache:
                return True

            _, cached_mtime, last_check = self._cache[symbol]
            now = datetime.now(ZoneInfo("America/New_York"))

            # Rate limit mtime checks
            if now - last_check < self.check_interval:
                return False

            # Check if file was modified
            try:
                current_mtime = file_path.stat().st_mtime
                return current_mtime > cached_mtime
            except FileNotFoundError:
                # File deleted -> reload to use defaults
                return True
            except Exception as e:
                logger.warning("Failed to check file mtime", symbol=symbol, error=str(e))
                return False

    def _load_from_file(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Load config from JSON file (without caching)."""
        file_path = self._get_config_path(symbol)

        try:
            if not file_path.exists():
                logger.debug("Config file not found", symbol=symbol, path=str(file_path))
                return None

            with open(file_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            logger.info("Config loaded from file", symbol=symbol, path=str(file_path))
            return config

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in config file", symbol=symbol, error=str(e), path=str(file_path))
            return None
        except Exception as e:
            logger.error("Failed to load config file", symbol=symbol, error=str(e), path=str(file_path))
            return None

    def load(self, symbol: str, base_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Load config for symbol with hot-reload support.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            base_config: Base config to merge with (from config.yml extras)

        Returns:
            Merged config dict (file overrides base)
        """
        symbol = symbol.upper()
        file_path = self._get_config_path(symbol)

        # Check if reload needed
        if self._should_reload(symbol, file_path):
            file_config = self._load_from_file(symbol)

            if file_config is not None:
                # Update cache
                try:
                    mtime = file_path.stat().st_mtime
                except Exception:
                    mtime = 0.0

                with self._lock:
                    self._cache[symbol] = (
                        file_config,
                        mtime,
                        datetime.now(ZoneInfo("America/New_York")),
                    )
            else:
                # File missing or invalid -> use base config only
                logger.warning("Using base config only", symbol=symbol)
                return base_config or {}

        # Retrieve from cache
        with self._lock:
            if symbol in self._cache:
                file_config, _, _ = self._cache[symbol]
                # Update last_check time
                _, mtime, _ = self._cache[symbol]
                self._cache[symbol] = (
                    file_config,
                    mtime,
                    datetime.now(ZoneInfo("America/New_York")),
                )
            else:
                # Cache miss (shouldn't happen after reload check)
                file_config = {}

        # Merge: base_config < file_config (file overrides)
        merged = self._deep_merge(base_config or {}, file_config)
        return merged

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deep merge two dicts (override takes precedence).

        Args:
            base: Base config dict
            override: Override config dict

        Returns:
            Merged dict
        """
        result = dict(base)

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                # Recursively merge nested dicts
                result[key] = ConfigLoader._deep_merge(result[key], value)
            else:
                # Override
                result[key] = value

        return result

    def invalidate_cache(self, symbol: Optional[str] = None) -> None:
        """
        Manually invalidate cache (for testing or forced reload).

        Args:
            symbol: Symbol to invalidate (None = invalidate all)
        """
        with self._lock:
            if symbol is None:
                self._cache.clear()
                logger.info("All cached configs invalidated")
            else:
                symbol = symbol.upper()
                if symbol in self._cache:
                    del self._cache[symbol]
                    logger.info("Config cache invalidated", symbol=symbol)

    def get_all_symbols(self) -> list[str]:
        """
        Discover all symbols with config files in config_dir.

        Returns:
            List of symbols (e.g., ["AAPL", "TSLA"])
        """
        try:
            pattern = f"{self.prefix}_*.json"
            files = self.config_dir.glob(pattern)
            symbols = []

            for file in files:
                # Extract symbol from filename: "enhanced_ml_AAPL.json" -> "AAPL"
                name = file.stem
                if name.startswith(self.prefix + "_"):
                    symbol = name[len(self.prefix) + 1 :]
                    symbols.append(symbol.upper())

            return sorted(symbols)

        except Exception as e:
            logger.error("Failed to discover symbols", error=str(e))
            return []


# Singleton instance (strategy can import this directly)
_global_loader: Optional[ConfigLoader] = None


def get_loader(
    config_dir: str = "strategies/configs",
    prefix: str = "enhanced_ml",
    check_interval_sec: float = 60.0,
) -> ConfigLoader:
    """
    Get or create global ConfigLoader singleton.

    ⚠️  IMPORTANT: This is a singleton pattern. The parameters (config_dir, prefix,
    check_interval_sec) are ONLY used when creating the instance for the first time.
    Subsequent calls will return the existing instance, ignoring any new parameters.

    If you need different configurations, either:
    1. Create a separate ConfigLoader instance directly (not recommended)
    2. Reset the singleton with reset_loader() before calling get_loader()
    3. Use consistent parameters across all calls (recommended)

    Args:
        config_dir: Config directory (default: "strategies/configs")
        prefix: File name prefix (default: "enhanced_ml")
        check_interval_sec: Hot-reload check interval (default: 60s)

    Returns:
        ConfigLoader instance (singleton)

    Example:
        >>> # First call creates the instance
        >>> loader = get_loader(config_dir="strategies/configs", prefix="enhanced_ml")
        >>> # Second call returns the SAME instance, ignoring new parameters
        >>> loader2 = get_loader(config_dir="other/dir", prefix="other")
        >>> assert loader is loader2  # Same instance!
    """
    global _global_loader
    if _global_loader is None:
        _global_loader = ConfigLoader(
            config_dir=config_dir,
            prefix=prefix,
            check_interval_sec=check_interval_sec,
        )
        logger.info(
            "ConfigLoader singleton created",
            config_dir=config_dir,
            prefix=prefix,
            check_interval_sec=check_interval_sec,
        )
    else:
        # Warn if parameters don't match existing instance
        if (
            str(_global_loader.config_dir) != config_dir
            or _global_loader.prefix != prefix
        ):
            logger.warning(
                "get_loader() called with different parameters, but singleton already exists. Using existing instance.",
                existing_config_dir=str(_global_loader.config_dir),
                existing_prefix=_global_loader.prefix,
                requested_config_dir=config_dir,
                requested_prefix=prefix,
            )
    return _global_loader


def reset_loader() -> None:
    """
    Reset the global ConfigLoader singleton.

    ⚠️  WARNING: Only use this for testing or when you need to reinitialize
    with different parameters. In production, this should rarely be needed.

    Example:
        >>> reset_loader()
        >>> loader = get_loader(config_dir="new/dir")
    """
    global _global_loader
    _global_loader = None
    logger.info("ConfigLoader singleton reset")
