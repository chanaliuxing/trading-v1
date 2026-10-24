"""
Strategy Status Tracker

提供状态跟踪功能，记录策略的最后更新时间，供 AI Brain 的 feature freshness 检测使用。

Usage:
    class MyStrategy(StrategyAlgorithm, StatusTrackerMixin):
        async def initialize(self):
            self.init_status_tracker()  # 初始化状态跟踪
            await self.ctx.subscribe_features(...)

        async def on_features(self, symbol: str, features: Dict):
            self.track_feature_update(symbol)  # 记录更新
            # ... 策略逻辑 ...

状态文件会写入: runs/strategies/status/{strategy_id}_status.json
格式: {
    "last_updated": timestamp,
    "symbols": {
        "AAPL": {"last_bar_ts": ts, "last_feature_ts": ts},
        ...
    }
}
"""

from __future__ import annotations
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional
from threading import Lock

from libs.logger import get_logger


class StatusTrackerMixin:
    """混入类，为策略添加状态跟踪功能"""

    def init_status_tracker(self) -> None:
        """初始化状态跟踪器 (在 strategy.initialize() 中调用)"""
        self._status_log = get_logger(f"strategy.{self.ctx.strategy_id}.status")
        self._status_lock = Lock()

        # 状态数据: {symbol: {last_bar_ts, last_feature_ts}}
        self._status_data: Dict[str, Dict[str, Optional[float]]] = {}

        # 状态文件路径
        self._status_dir = Path("runs/strategies/status")
        self._status_dir.mkdir(parents=True, exist_ok=True)
        self._status_file = self._status_dir / f"{self.ctx.strategy_id}_status.json"

        # 写入频率控制 (最多每5秒写一次，避免IO压力)
        self._last_write_ts = 0.0
        self._write_interval = 5.0  # seconds

        self._status_log.info("status_tracker_initialized",
                             strategy_id=self.ctx.strategy_id,
                             status_file=str(self._status_file))

    def track_bar_update(self, symbol: str, bar_ts: Optional[float] = None) -> None:
        """记录 bar 更新

        Args:
            symbol: 标的符号
            bar_ts: Bar 时间戳 (如果为 None,使用当前时间)
        """
        if not hasattr(self, '_status_data'):
            return  # 未初始化,跳过

        ts = bar_ts if bar_ts is not None else time.time()

        with self._status_lock:
            if symbol not in self._status_data:
                self._status_data[symbol] = {
                    "last_bar_ts": None,
                    "last_feature_ts": None
                }

            self._status_data[symbol]["last_bar_ts"] = ts

        self._maybe_write_status()

    def track_feature_update(self, symbol: str, feature_ts: Optional[float] = None) -> None:
        """记录特征更新

        Args:
            symbol: 标的符号
            feature_ts: 特征计算完成时间戳 (如果为 None,使用当前时间)
        """
        if not hasattr(self, '_status_data'):
            return

        ts = feature_ts if feature_ts is not None else time.time()

        with self._status_lock:
            if symbol not in self._status_data:
                self._status_data[symbol] = {
                    "last_bar_ts": None,
                    "last_feature_ts": None
                }

            self._status_data[symbol]["last_feature_ts"] = ts

        self._maybe_write_status()

    def _maybe_write_status(self) -> None:
        """条件性写入状态文件 (限频)"""
        now = time.time()

        # 限频: 最多每5秒写一次
        if now - self._last_write_ts < self._write_interval:
            return

        self._last_write_ts = now
        self._write_status()

    def _write_status(self) -> None:
        """写入状态文件 (原子写入)"""
        try:
            with self._status_lock:
                status = {
                    "last_updated": time.time(),
                    "strategy_id": self.ctx.strategy_id,
                    "symbols": self._status_data.copy()
                }

            # 原子写入: tmp → replace
            tmp_file = self._status_file.with_suffix(".json.tmp")
            with tmp_file.open("w") as f:
                json.dump(status, f, indent=2)

            tmp_file.replace(self._status_file)

        except Exception as e:
            self._status_log.warning("status_write_failed", error=str(e))

    def force_write_status(self) -> None:
        """强制立即写入状态 (在 shutdown 时调用)"""
        if hasattr(self, '_status_data'):
            self._write_status()


# Standalone status tracker for strategies that don't use mixin
class StrategyStatusTracker:
    """独立状态跟踪器 (如果不使用 Mixin)"""

    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self.log = get_logger(f"strategy.{strategy_id}.status")
        self.lock = Lock()

        self.status_data: Dict[str, Dict[str, Optional[float]]] = {}

        self.status_dir = Path("runs/strategies/status")
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self.status_file = self.status_dir / f"{strategy_id}_status.json"

        self.last_write_ts = 0.0
        self.write_interval = 5.0

        self.log.info("status_tracker_initialized",
                     strategy_id=strategy_id,
                     status_file=str(self.status_file))

    def track_bar(self, symbol: str, bar_ts: Optional[float] = None) -> None:
        """记录 bar 更新"""
        ts = bar_ts if bar_ts is not None else time.time()

        with self.lock:
            if symbol not in self.status_data:
                self.status_data[symbol] = {
                    "last_bar_ts": None,
                    "last_feature_ts": None
                }

            self.status_data[symbol]["last_bar_ts"] = ts

        self._maybe_write()

    def track_feature(self, symbol: str, feature_ts: Optional[float] = None) -> None:
        """记录特征更新"""
        ts = feature_ts if feature_ts is not None else time.time()

        with self.lock:
            if symbol not in self.status_data:
                self.status_data[symbol] = {
                    "last_bar_ts": None,
                    "last_feature_ts": None
                }

            self.status_data[symbol]["last_feature_ts"] = ts

        self._maybe_write()

    def _maybe_write(self) -> None:
        now = time.time()
        if now - self.last_write_ts < self.write_interval:
            return

        self.last_write_ts = now
        self._write()

    def _write(self) -> None:
        try:
            with self.lock:
                status = {
                    "last_updated": time.time(),
                    "strategy_id": self.strategy_id,
                    "symbols": self.status_data.copy()
                }

            tmp_file = self.status_file.with_suffix(".json.tmp")
            with tmp_file.open("w") as f:
                json.dump(status, f, indent=2)

            tmp_file.replace(self.status_file)

        except Exception as e:
            self.log.warning("status_write_failed", error=str(e))

    def force_write(self) -> None:
        """强制写入"""
        self._write()


# Self-test
if __name__ == "__main__":
    print("\n" + "="*80)
    print("StatusTracker Self-Test")
    print("="*80)

    # Test 1: Standalone tracker
    print("\n[Test 1] Standalone Tracker...")
    tracker = StrategyStatusTracker("test_strategy_001")

    print("  Tracking AAPL bar update...")
    tracker.track_bar("AAPL", time.time())

    print("  Tracking AAPL feature update...")
    tracker.track_feature("AAPL", time.time())

    print("  Tracking TSLA updates...")
    tracker.track_bar("TSLA", time.time() - 120)  # 2分钟前
    tracker.track_feature("TSLA", time.time() - 100)

    # Force write
    tracker.force_write()

    # Verify file
    assert tracker.status_file.exists(), "Status file should exist"
    with tracker.status_file.open("r") as f:
        data = json.load(f)

    print(f"  ✓ Status file written: {tracker.status_file}")
    print(f"  ✓ Tracked symbols: {list(data['symbols'].keys())}")

    assert "AAPL" in data["symbols"], "AAPL should be tracked"
    assert "TSLA" in data["symbols"], "TSLA should be tracked"
    assert data["symbols"]["AAPL"]["last_bar_ts"] is not None
    assert data["symbols"]["AAPL"]["last_feature_ts"] is not None

    print("  ✓ Status data correct")

    # Cleanup
    tracker.status_file.unlink()

    print("\n" + "="*80)
    print("✅ ALL TESTS PASSED")
    print("="*80)

    print("\nUsage:")
    print("  1. Mixin 方式 (推荐):")
    print("     class MyStrategy(StrategyAlgorithm, StatusTrackerMixin):")
    print("         async def initialize(self):")
    print("             self.init_status_tracker()")
    print("         async def on_features(self, symbol, features):")
    print("             self.track_feature_update(symbol)")
    print()
    print("  2. 独立跟踪器:")
    print("     tracker = StrategyStatusTracker('my_strategy_001')")
    print("     tracker.track_feature('AAPL')")
