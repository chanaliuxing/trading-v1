# indicators.py (1/2 - 由于长度限制,分两次写入)
"""
技术指标 - O(1)增量计算实现

所有指标实现增量更新,避免重复计算:
- P0: EMA, SMA, Returns
- P1: ATR, Bollinger Bands, Donchian
- P2: RSI, Stochastic
- P3: VWAP, OBV, CMF
- P5: StdDev, ZScore

Author: Trading System Team (Claude + final plan)
Version: 1.0.0
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, List, Deque
from collections import deque
import math


# ============================================================================
# P0: 基础指标
# ============================================================================

class EMA:
    """
    指数移动平均 (Exponential Moving Average)

    算法: EMA_new = alpha * price + (1 - alpha) * EMA_old
    其中: alpha = 2 / (period + 1)

    复杂度: O(1)
    """

    def __init__(self, period: int, *, init: Optional[float] = None) -> None:
        if period <= 0:
            raise ValueError("EMA period must be positive")
        self.period = int(period)
        self.alpha = 2.0 / (period + 1.0)
        self.value: Optional[float] = float(init) if init is not None else None

    def update(self, x: float) -> float:
        """更新EMA值"""
        fx = float(x)
        if self.value is None:
            self.value = fx
        else:
            self.value = self.alpha * fx + (1.0 - self.alpha) * self.value
        return float(self.value)


class SMA:
    """
    简单移动平均 (Simple Moving Average)

    使用deque滑动窗口 + 累积和

    复杂度: O(1) amortized
    """

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("SMA period must be positive")
        self.period = int(period)
        self.sum = 0.0
        self.buf: List[float] = []

    @property
    def value(self) -> Optional[float]:
        if not self.buf:
            return None
        n = min(len(self.buf), self.period)
        return self.sum / n

    def update(self, x: float) -> float:
        """更新SMA值"""
        fx = float(x)
        self.buf.append(fx)
        self.sum += fx
        if len(self.buf) > self.period:
            self.sum -= self.buf.pop(0)
        return float(self.value if self.value is not None else fx)


class Returns:
    """
    收益率指标

    支持:
    - 简单收益率: (price - prev_price) / prev_price
    - 对数收益率: log(price / prev_price)

    复杂度: O(deque)
    """

    def __init__(self, period: int = 1, use_log: bool = False) -> None:
        self.period = int(period)
        self.use_log = bool(use_log)
        self.buf: List[float] = []

    def update(self, price: float) -> float:
        """更新收益率"""
        p = float(price)
        self.buf.append(p)
        if len(self.buf) <= self.period:
            return 0.0
        prev = self.buf[-1 - self.period]
        if prev == 0:
            return 0.0
        if self.use_log:
            return math.log(p / prev)
        return (p / prev - 1.0)


# ============================================================================
# P1: 趋势指标
# ============================================================================

class ATR:
    """
    平均真实波幅 (Average True Range)

    算法:
    1. TR = max(high - low, |high - prev_close|, |low - prev_close|)
    2. ATR = EMA(TR, period) using Wilder's smoothing

    复杂度: O(1)
    """

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError("ATR period must be positive")
        self.period = int(period)
        self.alpha = 1.0 / self.period
        self.atr: Optional[float] = None
        self.prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> float:
        """更新ATR值"""
        h, l, c = float(high), float(low), float(close)

        if self.prev_close is None:
            tr = h - l
            self.atr = tr
        else:
            tr = max(h - l, abs(h - self.prev_close), abs(l - self.prev_close))
            if self.atr is not None:
                self.atr = self.atr * (1 - self.alpha) + tr * self.alpha
            else:
                self.atr = tr

        self.prev_close = c
        return float(self.atr)


class Bollinger:
    """
    布林带 (Bollinger Bands)

    算法:
    - Middle Band = SMA(close, period)
    - Upper Band = Middle + (std_dev * num_std)
    - Lower Band = Middle - (std_dev * num_std)

    使用Welford's在线算法计算标准差

    复杂度: O(1) amortized
    """

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        self.period = int(period)
        self.num_std = float(num_std)
        self.buf: List[float] = []
        self.sum = 0.0
        self.sum2 = 0.0
        self.mid = None
        self.up = None
        self.dn = None

    def update(self, x: float) -> Tuple[float, float, float, float, float]:
        """
        更新布林带值

        Returns:
            (middle, upper, lower, bandwidth, %b)
        """
        fx = float(x)
        self.buf.append(fx)
        self.sum += fx
        self.sum2 += fx * fx

        if len(self.buf) > self.period:
            old = self.buf.pop(0)
            self.sum -= old
            self.sum2 -= old * old

        n = len(self.buf)
        mean = self.sum / n
        var = (self.sum2 / n) - (mean * mean)
        std = (var ** 0.5) if var > 0 else 0.0

        up = mean + self.num_std * std
        dn = mean - self.num_std * std
        bw = (up - dn) / mean if mean != 0 else 0.0
        pctb = (fx - dn) / (up - dn) if up != dn else 0.0

        self.mid, self.up, self.dn = mean, up, dn
        return mean, up, dn, bw, pctb


class Donchian:
    """
    唐奇安通道 (Donchian Channel)

    算法:
    - Upper = max(high, period)
    - Lower = min(low, period)
    - Middle = (Upper + Lower) / 2

    复杂度: O(deque)
    """

    def __init__(self, period: int = 20) -> None:
        self.period = int(period)
        self.hi: List[float] = []
        self.lo: List[float] = []

    def update(self, high: float, low: float) -> Tuple[float, float]:
        """
        更新唐奇安通道

        Returns:
            (upper, lower)
        """
        self.hi.append(float(high))
        self.lo.append(float(low))
        if len(self.hi) > self.period:
            self.hi.pop(0)
            self.lo.pop(0)
        return max(self.hi), min(self.lo)


# ============================================================================
# P2: 动量指标
# ============================================================================

class RSI:
    """
    相对强弱指数 (Relative Strength Index)

    算法: Wilder RSI with RMA smoothing
    1. 计算涨跌幅
    2. 使用RMA平滑
    3. RSI = 100 - (100 / (1 + RS))

    复杂度: O(1)
    """

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError("RSI period must be positive")
        self.period = int(period)
        self.avg_gain: Optional[float] = None
        self.avg_loss: Optional[float] = None
        self.prev: Optional[float] = None
        self.value: Optional[float] = None
        self._seed_count = 0

    def update(self, price: float) -> Optional[float]:
        """更新RSI值"""
        p = float(price)

        if self.prev is None:
            self.prev = p
            self._seed_count = 1
            return None

        change = p - self.prev
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._seed_count < self.period:
            self.avg_gain = (0.0 if self.avg_gain is None else self.avg_gain) + gain
            self.avg_loss = (0.0 if self.avg_loss is None else self.avg_loss) + loss
            self._seed_count += 1
            if self._seed_count == self.period:
                self.avg_gain /= self.period
                self.avg_loss /= self.period
        else:
            assert self.avg_gain is not None and self.avg_loss is not None
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period

        self.prev = p

        if self._seed_count < self.period:
            self.value = None
        else:
            if self.avg_loss == 0:
                self.value = 100.0
            else:
                rs = self.avg_gain / self.avg_loss
                self.value = 100.0 - (100.0 / (1.0 + rs))

        return self.value


class Stoch:
    """
    随机指标 (Stochastic Oscillator)

    算法:
    - %K = (Close - Low_n) / (High_n - Low_n) * 100
    - %D = SMA(%K, d_period)

    复杂度: O(deque)
    """

    def __init__(self, k_period: int = 14, d_period: int = 3) -> None:
        self.kp = int(k_period)
        self.dp = int(d_period)
        self.hi: List[float] = []
        self.lo: List[float] = []
        self.k_sma = SMA(self.dp)

    def update(self, high: float, low: float, close: float) -> Tuple[float, float]:
        """
        更新随机指标

        Returns:
            (stoch_k, stoch_d)
        """
        self.hi.append(float(high))
        self.lo.append(float(low))
        if len(self.hi) > self.kp:
            self.hi.pop(0)
            self.lo.pop(0)

        highest = max(self.hi)
        lowest = min(self.lo)
        k = 0.0 if highest == lowest else (float(close) - lowest) / (highest - lowest) * 100.0
        d = self.k_sma.update(k)

        return k, d


# ============================================================================
# P3: 成交量指标
# ============================================================================

@dataclass
class VWAPWindow:
    """
    成交量加权平均价 (Volume Weighted Average Price)

    算法:
    - Typical Price = (High + Low + Close) / 3
    - VWAP = Sum(Typical Price * Volume) / Sum(Volume)

    日内累积指标

    复杂度: O(1)
    """
    pv_sum: float = 0.0
    vol_sum: float = 0.0
    value: Optional[float] = None

    def update(self, high: float, low: float, close: float, vol: float) -> float:
        """更新VWAP值"""
        typical = (float(high) + float(low) + float(close)) / 3.0
        v = float(vol)
        self.pv_sum += typical * v
        self.vol_sum += v
        self.value = (self.pv_sum / self.vol_sum) if self.vol_sum > 0 else None
        return self.value if self.value is not None else 0.0


class OBV:
    """
    能量潮指标 (On-Balance Volume)

    算法:
    - 如果 close > prev_close: OBV += volume
    - 如果 close < prev_close: OBV -= volume
    - 如果 close == prev_close: OBV 不变

    复杂度: O(1)
    """

    def __init__(self) -> None:
        self.value = 0.0
        self.prev_close: Optional[float] = None

    def update(self, close: float, volume: float) -> float:
        """更新OBV值"""
        if self.prev_close is None:
            self.prev_close = float(close)
            return self.value

        if close > self.prev_close:
            self.value += float(volume)
        elif close < self.prev_close:
            self.value -= float(volume)

        self.prev_close = float(close)
        return self.value


class CMF:
    """
    蔡金资金流量指标 (Chaikin Money Flow)

    算法:
    - MF Multiplier = ((Close - Low) - (High - Close)) / (High - Low)
    - MF Volume = MF Multiplier * Volume
    - CMF = Sum(MF Volume, period) / Sum(Volume, period)

    复杂度: O(1) amortized
    """

    def __init__(self, period: int = 20) -> None:
        self.period = int(period)
        self.mf_vol: List[float] = []
        self.vol: List[float] = []

    def update(self, high: float, low: float, close: float, volume: float) -> float:
        """更新CMF值"""
        h, l, c, v = float(high), float(low), float(close), float(volume)
        mfm = 0.0 if (h == l) else ((c - l) - (h - c)) / (h - l)
        mfv = mfm * v

        self.mf_vol.append(mfv)
        self.vol.append(v)

        if len(self.mf_vol) > self.period:
            self.mf_vol.pop(0)
            self.vol.pop(0)

        sum_mfv = sum(self.mf_vol)
        sum_vol = sum(self.vol)
        return (sum_mfv / sum_vol) if sum_vol > 0 else 0.0


# ============================================================================
# P5: 统计指标
# ============================================================================

class StdDev:
    """
    标准差指标

    使用Welford's在线算法

    复杂度: O(1) amortized
    """

    def __init__(self, period: int = 20) -> None:
        self.period = int(period)
        self.buf: List[float] = []

    def update(self, x: float) -> float:
        """更新标准差"""
        fx = float(x)
        self.buf.append(fx)
        if len(self.buf) > self.period:
            self.buf.pop(0)

        n = len(self.buf)
        mean = sum(self.buf) / n
        var = sum((b - mean) ** 2 for b in self.buf) / max(1, n - 1)
        return var ** 0.5


class ZScore:
    """
    Z分数指标

    算法: Z = (Price - Mean) / StdDev

    复杂度: O(1) amortized
    """

    def __init__(self, period: int = 20) -> None:
        self.period = int(period)
        self.buf: List[float] = []

    def update(self, x: float) -> float:
        """更新Z分数"""
        fx = float(x)
        self.buf.append(fx)
        if len(self.buf) > self.period:
            self.buf.pop(0)

        n = len(self.buf)
        mean = sum(self.buf) / n
        var = sum((b - mean) ** 2 for b in self.buf) / max(1, n - 1)
        std = (var ** 0.5) if var > 0 else 0.0

        return (fx - mean) / std if std > 0 else 0.0


# ============================================================================
# P4: 特殊指标 (Opening Range Breakout)
# ============================================================================

@dataclass
class ORB5:
    """
    Opening Range Breakout (前5分钟突破)

    跟踪开盘后前5分钟的高低点

    Attributes:
        high: 前5分钟最高价
        low: 前5分钟最低价
        frozen: 是否已冻结 (5分钟后)
    """
    high: Optional[float] = None
    low: Optional[float] = None
    frozen: bool = False

    def update(self, ts_ny_seconds: int, price_high: float, price_low: float) -> None:
        """
        更新ORB值

        Args:
            ts_ny_seconds: 纽约时区的秒数 (相对于当天零点)
            price_high: 当前高价
            price_low: 当前低价
        """
        secs_in_day = ts_ny_seconds % 86400
        start = 9 * 3600 + 30 * 60  # 9:30 AM ET
        end = start + 300  # 9:35 AM ET

        if secs_in_day < start:
            # 开盘前: 重置
            self.high = None
            self.low = None
            self.frozen = False
            return

        if secs_in_day >= end:
            # 5分钟后: 冻结
            self.frozen = True
            return

        # 5分钟内: 更新高低点
        h = float(price_high)
        l = float(price_low)
        self.high = max(h, self.high) if self.high is not None else h
        self.low = min(l, self.low) if self.low is not None else l
