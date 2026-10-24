# feature_registry.py
"""
特征注册表 - 管理按需特征流水线

解析feature_configs -> 为不同TF构建指标流水线
统一缓存最新特征值

Author: Trading System Team (based on final plan)
Version: 1.0.0
"""

from __future__ import annotations
from typing import Any, Dict, List, Iterable, Set
from collections import defaultdict
from dataclasses import dataclass, field
import structlog

from .indicator_registry import IndicatorRegistry, BaseIndicatorAdapter
from .indicators import ORB5

logger = structlog.get_logger(__name__)


@dataclass
class _SymbolTFPipeline:
    """单个symbol的单个TF的指标流水线"""
    indicators: List[BaseIndicatorAdapter] = field(default_factory=list)


class FeatureRegistry:
    """
    特征注册表

    职责:
    - 解析feature_configs
    - 为不同TF构建指标流水线
    - 统一缓存最新特征值
    """

    def __init__(
        self,
        feature_configs: Iterable[Dict[str, Any]] | None = None,
        default_tf: str = "1m"
    ) -> None:
        self._pipelines = defaultdict(lambda: defaultdict(_SymbolTFPipeline))
        self._last_features = defaultdict(lambda: defaultdict(dict))
        self._orb5 = defaultdict(ORB5)
        self._default_tf = default_tf
        self._raw_feature_configs = list(feature_configs or [])
        self._available = IndicatorRegistry.list_available()
        self._built_symbols: Set[str] = set()  # 记录已构建的symbols

    def build_for_symbol(self, symbol: str) -> None:
        """按symbol初始化流水线"""
        if symbol in self._built_symbols:
            return

        indicator_count = 0
        for cfg in self._raw_feature_configs or self.default_feature_configs():
            name = cfg.get("name")
            if name not in self._available:
                logger.warning("indicator_not_available", name=name, symbol=symbol)
                continue

            tf = cfg.get("tf", self._default_tf)
            adapter = IndicatorRegistry.create(name, cfg, tf)
            if not adapter:
                continue

            self._pipelines[symbol][tf].indicators.append(adapter)
            indicator_count += 1

        self._built_symbols.add(symbol)
        logger.info(
            "feature_pipeline_built",
            symbol=symbol,
            indicator_count=indicator_count,
            timeframes=list(self._pipelines[symbol].keys())
        )

    def on_finalized_bar(self, symbol: str, tf: str, bar: Dict[str, Any]) -> Dict[str, Any]:
        """
        当一个bar完成时,运行该TF的指标流水线

        Args:
            symbol: 交易标的
            tf: 时间框架
            bar: 完成的bar

        Returns:
            计算出的特征字典
        """
        self.build_for_symbol(symbol)
        out: Dict[str, Any] = {}

        # 运行该TF的流水线
        pipe = self._pipelines[symbol].get(tf)
        if pipe:
            for ind in pipe.indicators:
                out.update(ind.update(bar))

        # ORB5 (仅在1m触发)
        if tf == "1m":
            orb = self._orb5[symbol]
            orb.update(int(bar["ts"]), bar["high"], bar["low"])
            out.update({
                "orb5_hi": orb.high if orb.high is not None else float("nan"),
                "orb5_lo": orb.low if orb.low is not None else float("nan"),
                "orb5_frozen": orb.frozen
            })

        if out:
            self._last_features[symbol][tf].update(out)

        return out

    def snapshot_features(self, symbol: str) -> Dict[str, Dict[str, Any]]:
        """获取symbol的所有TF特征快照"""
        return self._last_features.get(symbol, {})

    def get_features(self, symbol: str, tf: str) -> Dict[str, Any]:
        """获取symbol的特定TF特征"""
        return self._last_features.get(symbol, {}).get(tf, {})

    @staticmethod
    def default_feature_configs() -> List[Dict[str, Any]]:
        """默认特征配置"""
        return [
            {"name": "ema", "period": 20, "source": "close", "tf": "1m"},
            {"name": "rsi", "period": 14, "source": "close", "tf": "1m"},
            {"name": "atr", "period": 14, "tf": "1m"},
            {"name": "vwap", "tf": "1m"},
        ]
