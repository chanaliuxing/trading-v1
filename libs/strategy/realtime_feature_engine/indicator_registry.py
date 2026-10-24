# indicator_registry.py
"""
指标注册表 - 插件化指标管理

支持动态注册和创建指标适配器

Author: Trading System Team (based on Codex + final plan)
Version: 1.0.0
"""

from __future__ import annotations
from typing import Dict, Any, Optional, Type
import structlog

from .indicators import (
    EMA, SMA, RSI, ATR, VWAPWindow, OBV, Donchian, Bollinger,
    CMF, StdDev, ZScore, Stoch, Returns, ORB5
)

logger = structlog.get_logger(__name__)


class BaseIndicatorAdapter:
    """
    指标适配器基类

    统一适配：以 bar(dict) -> {feature_key: value} 输出
    子类在 __init__(config, tf) 中确定输出名（含 period/source/tf）
    """

    def __init__(self, config: Dict[str, Any], tf: str) -> None:
        self.tf = tf

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        raise NotImplementedError


def _name_with(src: str, base: str, period: Any, tf: str, extra: str = "") -> str:
    """
    构建特征名称

    格式: {base}_{period}_{source}_{extra}_{tf}
    例如: ema_20_close_1m
    """
    parts = [base]
    if period is not None:
        parts.append(str(period))
    if src:
        parts.append(str(src))
    if extra:
        parts.append(extra)
    parts.append(tf)
    return "_".join(parts)


class IndicatorRegistry:
    """
    指标注册表

    职责:
    1. 维护指标名称到适配器类的映射
    2. 提供装饰器用于注册
    3. 工厂方法创建指标实例
    """

    _registry: Dict[str, Type[BaseIndicatorAdapter]] = {}

    @classmethod
    def register(cls, name: str):
        """装饰器: 注册指标适配器"""
        def decorator(indicator_class: Type[BaseIndicatorAdapter]):
            if name in cls._registry:
                logger.warning(
                    "indicator_already_registered",
                    name=name,
                    old_class=cls._registry[name].__name__,
                    new_class=indicator_class.__name__
                )
            cls._registry[name] = indicator_class
            logger.debug("indicator_registered", name=name, class_name=indicator_class.__name__)
            return indicator_class
        return decorator

    @classmethod
    def create(cls, name: str, config: Dict[str, Any], tf: str) -> Optional[BaseIndicatorAdapter]:
        """工厂方法: 创建指标实例"""
        klass = cls._registry.get(name)
        if not klass:
            logger.error("indicator_not_found", name=name, available=list(cls._registry.keys()))
            return None
        return klass(config, tf)

    @classmethod
    def list_available(cls) -> Dict[str, str]:
        """列出所有已注册的指标"""
        return {k: v.__name__ for k, v in cls._registry.items()}


# ============================================================================
# 内置指标适配器
# ============================================================================

@IndicatorRegistry.register("ema")
class EMAAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 20))
        self.impl = EMA(self.period)
        self.key = _name_with(self.src, "ema", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar[self.src])}


@IndicatorRegistry.register("sma")
class SMAAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 20))
        self.impl = SMA(self.period)
        self.key = _name_with(self.src, "sma", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar[self.src])}


@IndicatorRegistry.register("rsi")
class RSIAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 14))
        self.impl = RSI(self.period)
        self.key = _name_with(self.src, "rsi", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        val = self.impl.update(bar[self.src])
        return {self.key: float("nan") if val is None else float(val)}


@IndicatorRegistry.register("atr")
class ATRAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.period = int(config.get("period", 14))
        self.impl = ATR(self.period)
        self.key = _name_with("", "atr", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar["high"], bar["low"], bar["close"])}


@IndicatorRegistry.register("vwap")
class VWAPAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.impl = VWAPWindow()
        self.key = _name_with("", "vwap", None, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar["high"], bar["low"], bar["close"], bar.get("volume", 0.0))}


@IndicatorRegistry.register("obv")
class OBVAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.impl = OBV()
        self.key = _name_with("", "obv", None, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar["close"], bar.get("volume", 0.0))}


@IndicatorRegistry.register("donchian")
class DonchianAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.period = int(config.get("period", 20))
        self.impl = Donchian(self.period)
        self.hi_key = _name_with("", "donchian_hi", self.period, tf)
        self.lo_key = _name_with("", "donchian_lo", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        hi, lo = self.impl.update(bar["high"], bar["low"])
        return {self.hi_key: hi, self.lo_key: lo}


@IndicatorRegistry.register("bb")
class BollingerAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 20))
        self.num_std = float(config.get("num_std", 2.0))
        self.impl = Bollinger(self.period, self.num_std)
        self.mid_key = _name_with(self.src, "bb_mid", self.period, tf)
        self.up_key = _name_with(self.src, "bb_up", self.period, tf)
        self.dn_key = _name_with(self.src, "bb_dn", self.period, tf)
        self.bw_key = _name_with(self.src, "bb_bw", self.period, tf)
        self.pctb_key = _name_with(self.src, "bb_pctb", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        mid, up, dn, bw, pctb = self.impl.update(bar[self.src])
        return {
            self.mid_key: mid,
            self.up_key: up,
            self.dn_key: dn,
            self.bw_key: bw,
            self.pctb_key: pctb
        }


@IndicatorRegistry.register("cmf")
class CMFAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.period = int(config.get("period", 20))
        self.impl = CMF(self.period)
        self.key = _name_with("", "cmf", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        val = self.impl.update(bar["high"], bar["low"], bar["close"], bar.get("volume", 0.0))
        return {self.key: val}


@IndicatorRegistry.register("std")
class StdAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 20))
        self.impl = StdDev(self.period)
        self.key = _name_with(self.src, "std", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar[self.src])}


@IndicatorRegistry.register("zscore")
class ZScoreAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 20))
        self.impl = ZScore(self.period)
        self.key = _name_with(self.src, "zscore", self.period, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar[self.src])}


@IndicatorRegistry.register("stoch")
class StochAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.kp = int(config.get("k_period", 14))
        self.dp = int(config.get("d_period", 3))
        self.impl = Stoch(self.kp, self.dp)
        self.k_key = _name_with("", "stoch_k", self.kp, tf)
        self.d_key = _name_with("", "stoch_d", self.dp, tf)

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        k, d = self.impl.update(bar["high"], bar["low"], bar["close"])
        return {self.k_key: k, self.d_key: d}


@IndicatorRegistry.register("returns")
class ReturnsAdapter(BaseIndicatorAdapter):
    def __init__(self, config: Dict[str, Any], tf: str):
        super().__init__(config, tf)
        self.src = config.get("source", "close")
        self.period = int(config.get("period", 1))
        self.use_log = bool(config.get("use_log", False))
        self.impl = Returns(self.period, self.use_log)
        self.key = _name_with(self.src, "ret", self.period, tf, "log" if self.use_log else "")

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        return {self.key: self.impl.update(bar[self.src])}
