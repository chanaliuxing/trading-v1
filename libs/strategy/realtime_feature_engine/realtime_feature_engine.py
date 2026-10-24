# realtime_feature_engine.py
"""
实时特征引擎 - 核心协调器

统一管理:
- 多时间框架聚合 (MTFAggregator)
- 特征流水线 (FeatureRegistry)
- 回调触发 (emit_on去抖)

Author: Trading System Team (final plan)
Version: 1.0.0
"""

from __future__ import annotations
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional
import asyncio
import structlog

from .mtf_aggregator import MTFAggregator
from .feature_registry import FeatureRegistry

Callback = Callable[[str, Dict[str, Any]], Awaitable[None] | None]
logger = structlog.get_logger(__name__)


class RealtimeFeatureEngine:
    """
    实时特征引擎

    特性:
    - 多时间框架严格自然桶聚合 (去抖)
    - 插件化指标流水线 (按需)
    - emit_on: 只在指定TF完成时触发策略回调

    使用示例:
        engine = RealtimeFeatureEngine(
            symbols=["AAPL", "TSLA"],
            feature_configs=[
                {'name': 'ema', 'period': 20, 'tf': '1m'},
                {'name': 'rsi', 'period': 14, 'tf': '1m'},
            ],
            emit_on="1m"
        )

        engine.register_callback(my_strategy.on_features)
        await engine.warm_up_with_5s(historical_data)

        async for bar_1s in stream:
            await engine.on_1s_bar("AAPL", bar_1s)
    """

    def __init__(
        self,
        *,
        symbols: Iterable[str],
        feature_configs: Iterable[Dict[str, Any]] | None = None,
        timeframes: Iterable[str] = ("1s", "1m", "5m", "15m"),
        emit_on: str = "1m",
        lookbacks: Optional[Dict[str, int]] = None
    ) -> None:
        """
        初始化引擎

        Args:
            symbols: 交易标的列表
            feature_configs: 特征配置列表
            timeframes: 支持的时间框架
            emit_on: 触发回调的时间框架
            lookbacks: 每个TF保留的历史bar数量
        """
        self.symbols = list(symbols)
        self.emit_on = emit_on

        # 多周期聚合器
        self.aggregator = MTFAggregator(timeframes=timeframes, lookbacks=lookbacks)

        # 特征注册表
        self.features = FeatureRegistry(feature_configs=feature_configs, default_tf=emit_on)

        # 回调函数列表
        self._callbacks: List[Callback] = []

        # 去抖: 记录每个symbol最后emitted的桶end
        self._last_emitted_bucket: Dict[str, int] = {}

        logger.info(
            "realtime_feature_engine_initialized",
            symbols=self.symbols,
            emit_on=self.emit_on,
            timeframes=list(timeframes)
        )

    def register_callback(self, fn: Callback) -> None:
        """
        注册特征更新回调

        Args:
            fn: 回调函数,签名 callback(symbol: str, features: Dict)
        """
        if not callable(fn):
            raise TypeError("callback must be callable")
        self._callbacks.append(fn)
        logger.info("callback_registered", callback=fn.__name__)

    async def warm_up_with_5s(self, seeds: Dict[str, List[Dict[str, Any]]]) -> None:
        """
        从5s历史数据预热

        Args:
            seeds: {symbol: [bars_5s]}
        """
        for sym, bars in (seeds or {}).items():
            self.aggregator.seed_from_5s(sym, bars)
        logger.info("warmup_complete", symbols=list(seeds.keys()) if seeds else [])

    async def on_1s_bar(self, symbol: str, bar_1s: Dict[str, Any]) -> None:
        """
        推送单个1s bar

        Args:
            symbol: 交易标的
            bar_1s: 1秒bar数据
        """
        finalized = self.aggregator.update_1s(symbol, bar_1s)
        await self._handle_finalized(symbol, finalized)

    async def process_1s_bars(self, symbol: str, bars_1s: Iterable[Dict[str, Any]]) -> None:
        """
        批量处理1s bars

        Args:
            symbol: 交易标的
            bars_1s: 1秒bars列表
        """
        for b in bars_1s:
            finalized = self.aggregator.update_1s(symbol, b)
            await self._handle_finalized(symbol, finalized)

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        """
        获取当前快照 (所有TF的形成中bars + 特征)

        Args:
            symbol: 交易标的

        Returns:
            {tf: {bar_data, features}}
        """
        snap = self.aggregator.snapshot(symbol)
        feats = self.features.snapshot_features(symbol)

        for tf, bucket in snap.items():
            bucket["features"] = feats.get(tf, {})

        return snap

    def get_features(self, symbol: str, tf: str) -> Dict[str, Any]:
        """
        获取特定TF的特征

        Args:
            symbol: 交易标的
            tf: 时间框架

        Returns:
            特征字典
        """
        return self.features.get_features(symbol, tf)

    async def _handle_finalized(
        self,
        symbol: str,
        finalized: Dict[str, Dict[str, float]]
    ) -> None:
        """
        处理完成的bars

        Args:
            symbol: 交易标的
            finalized: 完成的bars {tf: bar}
        """
        if not finalized:
            return

        # 1. 更新所有完成TF的特征
        for tf, bar in finalized.items():
            self.features.on_finalized_bar(symbol, tf, bar)

        # 2. 根据emit_on发射回调 (去抖)
        emit_bar = finalized.get(self.emit_on)
        if not emit_bar:
            return

        bucket_end = int(emit_bar["ts"])
        last = self._last_emitted_bucket.get(symbol, -1)
        if last >= bucket_end:
            return

        self._last_emitted_bucket[symbol] = bucket_end

        # 3. 触发所有回调
        feats = dict(self.features.get_features(symbol, self.emit_on) or {})
        # Ensure timestamp is included for downstream alignment guards
        # Use microseconds to align with strategy expectations
        try:
            feats_ts_us = int(bucket_end) * 1_000_000
            feats["ts"] = feats_ts_us
        except Exception:
            # Best-effort; do not block callbacks
            pass
        # Attach OHLCV from the finalized bar for this emit (so sinks/backtests have full row)
        try:
            feats_bar = emit_bar
            for k in ("open", "high", "low", "close", "volume", "trades"):
                if k in feats_bar and k not in feats:
                    feats[k] = feats_bar[k]
        except Exception:
            pass
        for cb in self._callbacks:
            try:
                maybe_coro = cb(symbol, feats)
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception as e:
                logger.exception("callback_failed", symbol=symbol, error=str(e), exc_info=True)
