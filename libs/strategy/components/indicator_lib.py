"""
IndicatorLib Component (Strict Optional Semantics)
Ensures data-insufficient states return None, not 0.0.
"""
import statistics
import math
from typing import List, Optional, Deque, Any

class IndicatorLib:
    @staticmethod
    def weighted_moving_average(values: List[float], period: int) -> Optional[float]:
        """Return the latest weighted moving average, or None when not warmed up."""
        if period <= 0:
            raise ValueError("WMA period must be positive")
        if len(values) < period:
            return None
        window = [float(x) for x in values[-period:]]
        denom = period * (period + 1) / 2.0
        return math.fsum((idx + 1) * value for idx, value in enumerate(window)) / denom

    @staticmethod
    def hma(values: List[float], period: int) -> Optional[float]:
        """Return the latest Hull moving average, or None when not warmed up."""
        series = IndicatorLib.hma_series(values, period)
        return series[-1] if series else None

    @staticmethod
    def hma_series(values: List[float], period: int) -> List[Optional[float]]:
        """Hull moving average series using WMA(2*WMA(n/2)-WMA(n), sqrt(n))."""
        if period <= 0:
            raise ValueError("HMA period must be positive")
        src = [float(x) for x in values]
        half = max(1, period // 2)
        root = max(1, int(math.sqrt(period)))
        raw: List[Optional[float]] = []
        raw_values: List[float] = []
        out: List[Optional[float]] = []
        for idx in range(len(src)):
            prefix = src[: idx + 1]
            wma_half = IndicatorLib.weighted_moving_average(prefix, half)
            wma_full = IndicatorLib.weighted_moving_average(prefix, period)
            if wma_half is None or wma_full is None:
                raw.append(None)
                out.append(None)
                continue
            raw_value = 2.0 * wma_half - wma_full
            raw.append(raw_value)
            raw_values.append(raw_value)
            if len(raw_values) < root:
                out.append(None)
            else:
                out.append(IndicatorLib.weighted_moving_average(raw_values, root))
        return out

    @staticmethod
    def atr(bars: List[Any], period: int = 14) -> Optional[float]:
        """Calculates Wilder's ATR. Returns None if bars < period + 1."""
        if len(bars) < period + 1: return None
        
        def calc_tr(curr, prev_close):
            return max(curr.high - curr.low, abs(curr.high - prev_close), abs(curr.low - prev_close))

        trs = []
        p_close = bars[0].close
        for b in bars[1:]:
            trs.append(calc_tr(b, p_close))
            p_close = b.close

        if len(trs) < period: return None
        atr = math.fsum(trs[:period]) / float(period)
        for x in trs[period:]:
            atr = (atr * (period - 1) + x) / float(period)
        return float(atr)

    @staticmethod
    def vwap(bars: Deque[Any]) -> Optional[float]:
        """Calculates cumulative VWAP. Returns None if volume is zero."""
        if not bars: return None
        pv = math.fsum(getattr(b, "pv_sum", b.close * b.volume) for b in bars)
        vv = math.fsum(b.volume for b in bars)
        if vv < 1e-9: return None # V1 Alignment: Strict None for zero data
        return pv / vv

    @staticmethod
    def volume_cv(vol_series: List[float]) -> Optional[float]:
        if len(vol_series) < 2: return None
        mu = statistics.mean(vol_series)
        if mu <= 0: return None
        sigma = statistics.pstdev(vol_series)
        return min(2.0, float(sigma / mu))

    @staticmethod
    def volume_surge(current_vol: float, rolling_volumes: List[float]) -> Optional[float]:
        if not rolling_volumes: return None
        avg_vol = math.fsum(rolling_volumes) / len(rolling_volumes)
        if avg_vol <= 0: return None
        return current_vol / avg_vol
