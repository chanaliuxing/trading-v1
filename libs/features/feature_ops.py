# feature_ops.py
# SPDX-License-Identifier: Apache-2.0
#
# 机构级特征算子注册与实现（零-deepcopy，严格后视）
# 依赖：pandas, numpy；scipy 可选（IC 不在本文件）
from __future__ import annotations
from typing import Callable, Dict, Any, List, Optional
import math
import numpy as np
import pandas as pd

_OP_REGISTRY: Dict[str, Callable[..., pd.Series | pd.DataFrame]] = {}

def register_op(name: str):
    """装饰器：注册一个算子到全局表。"""
    def deco(fn: Callable[..., pd.Series | pd.DataFrame]):
        _OP_REGISTRY[name] = fn
        return fn
    return deco

def get_op(name: str) -> Callable[..., pd.Series | pd.DataFrame]:
    if name not in _OP_REGISTRY:
        raise KeyError(f"Unknown op: {name}")
    return _OP_REGISTRY[name]

# ---------------------
# 工具方法（后视安全、分组滚动）
# ---------------------
def _ensure_sorted(df: pd.DataFrame, by: str = "ts") -> pd.DataFrame:
    if not df.index.is_monotonic_increasing or (by in df.columns and not df[by].is_monotonic_increasing):
        return df.sort_values(by=[by] if by in df.columns else None).reset_index(drop=True)
    return df

def _group(df: pd.DataFrame, col: str = "symbol"):
    if col not in df.columns:
        raise KeyError(f"Missing column `{col}` for per_symbol operations.")
    return df.groupby(col, sort=False, group_keys=False)

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=1).mean()

def _rolling(s: pd.Series, window: int, fn: str) -> pd.Series:
    r = s.rolling(window=window, min_periods=1)
    return getattr(r, fn)()

# ---------------------
# 基础变换
# ---------------------
@register_op("log1p")
def op_log1p(df: pd.DataFrame, col: str, **_) -> pd.Series:
    return np.log1p(df[col])

@register_op("clip")
def op_clip(df: pd.DataFrame, col: str, lower: Optional[float] = None, upper: Optional[float] = None, **_) -> pd.Series:
    s = df[col]
    if lower is not None:
        s = s.clip(lower=lower)
    if upper is not None:
        s = s.clip(upper=upper)
    return s

@register_op("power")
def op_power(df: pd.DataFrame, col: str, p: float = 1.0, **_) -> pd.Series:
    return df[col] ** float(p)

@register_op("sign")
def op_sign(df: pd.DataFrame, col: str, **_) -> pd.Series:
    return np.sign(df[col])

@register_op("rank_pct")
def op_rank_pct(df: pd.DataFrame, col: str, per_symbol: bool = True, **_) -> pd.Series:
    if per_symbol:
        return _group(df).apply(lambda g: g[col].rank(pct=True))
    return df[col].rank(pct=True)

# ---------------------
# 时间序列算子：严格后视
# ---------------------
@register_op("lag")
def op_lag(df: pd.DataFrame, col: str, lags: int = 1, per_symbol: bool = True, **_) -> pd.Series:
    if lags < 1:
        raise ValueError("lags must be >= 1")
    if per_symbol:
        return _group(df).apply(lambda g: g[col].shift(lags))
    return df[col].shift(lags)

@register_op("diff")
def op_diff(df: pd.DataFrame, col: str, periods: int = 1, per_symbol: bool = True, **_) -> pd.Series:
    if per_symbol:
        return _group(df).apply(lambda g: g[col].diff(periods))
    return df[col].diff(periods)

@register_op("rolling_sum")
def op_rolling_sum(df: pd.DataFrame, col: str, window_bars: int, per_symbol: bool = True, **_) -> pd.Series:
    if per_symbol:
        return _group(df).apply(lambda g: _rolling(g[col], window_bars, "sum"))
    return _rolling(df[col], window_bars, "sum")

@register_op("rolling_mean")
def op_rolling_mean(df: pd.DataFrame, col: str, window_bars: int, per_symbol: bool = True, **_) -> pd.Series:
    if per_symbol:
        return _group(df)[col].transform(lambda s: _rolling(s, window_bars, "mean"))
    return _rolling(df[col], window_bars, "mean")

@register_op("rolling_std")
def op_rolling_std(df: pd.DataFrame, col: str, window_bars: int, per_symbol: bool = True, **_) -> pd.Series:
    if per_symbol:
        return _group(df).apply(lambda g: _rolling(g[col], window_bars, "std"))
    return _rolling(df[col], window_bars, "std")

@register_op("rolling_zscore")
def op_rolling_zscore(df: pd.DataFrame, col: str, window_bars: int, per_symbol: bool = True, eps: float = 1e-9, **_) -> pd.Series:
    def _z(g: pd.Series):
        r = g.rolling(window_bars, min_periods=1)
        mean = r.mean()
        std = r.std().replace(0.0, np.nan)
        out = (g - mean) / (std + eps)
        return out
    if per_symbol:
        return _group(df).apply(lambda g: _z(g[col]))
    return _z(df[col])

@register_op("ema")
def op_ema(df: pd.DataFrame, col: str, span: int, per_symbol: bool = True, **_) -> pd.Series:
    if per_symbol:
        return _group(df).apply(lambda g: _ema(g[col], span))
    return _ema(df[col], span)

# ---------------------
# 组合运算
# ---------------------
@register_op("ratio")
def op_ratio(df: pd.DataFrame, num: str, den: str, eps: float = 1e-12, **_) -> pd.Series:
    return df[num] / (df[den] + eps)

@register_op("ratio_minus_1")
def op_ratio_minus_1(df: pd.DataFrame, num: str, den: str, eps: float = 1e-12, **_) -> pd.Series:
    return (df[num] / (df[den] + eps)) - 1.0

@register_op("sum")
def op_sum(df: pd.DataFrame, a: str, b: str, **_) -> pd.Series:
    return df[a] + df[b]

@register_op("product")
def op_product(df: pd.DataFrame, a: str, b: str, **_) -> pd.Series:
    return df[a] * df[b]

@register_op("min")
def op_min(df: pd.DataFrame, a: str, b: str, **_) -> pd.Series:
    return np.minimum(df[a], df[b])

@register_op("max")
def op_max(df: pd.DataFrame, a: str, b: str, **_) -> pd.Series:
    return np.maximum(df[a], df[b])

# ---------------------
# 价量/微结构/技术指标（轻量实现）
# ---------------------
@register_op("vwap_distance")
def op_vwap_distance(df: pd.DataFrame, price_col: str = "close", vol_col: str = "volume",
                     per_symbol: bool = True, window_bars: int = 20, eps: float = 1e-9, **_) -> pd.Series:
    # 近似窗口内 VWAP 偏离（训练/特征用途，严格后视）
    def _fn(g: pd.DataFrame):
        pv = (g[price_col] * g[vol_col]).rolling(window_bars, min_periods=1).sum()
        v = g[vol_col].rolling(window_bars, min_periods=1).sum()
        vwap = pv / (v + eps)
        return (g[price_col] - vwap)
    if per_symbol:
        return _group(df).apply(_fn)
    return _fn(df)

@register_op("effective_spread")
def op_effective_spread(df: pd.DataFrame, high_col: str = "high", low_col: str = "low", mid_col: str = "close", **_) -> pd.Series:
    # 近似：高低区间相对"中价"的幅度
    return (df[high_col] - df[low_col]).abs() / (df[mid_col].abs() + 1e-9)

@register_op("amihud_illiquidity")
def op_amihud(df: pd.DataFrame, ret_col: str = "ret_1", vol_col: str = "volume", eps: float = 1e-12, **_) -> pd.Series:
    # |ret| / volume
    v = (df[vol_col].abs() + eps)
    return df[ret_col].abs() / v

@register_op("price_impact_hl")
def op_price_impact_hl(df: pd.DataFrame, close_col: str = "close", high_col: str = "high", low_col: str = "low", **_) -> pd.Series:
    mid_hl = (df[high_col] + df[low_col]) / 2.0
    return mid_hl - df[close_col].shift(1)

@register_op("ta.rsi")
def op_ta_rsi(df: pd.DataFrame, col: str = "close", window_bars: int = 14, per_symbol: bool = True, **_) -> pd.Series:
    def _rsi(s: pd.Series, n: int):
        delta = s.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1/n, min_periods=1, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/n, min_periods=1, adjust=False).mean()
        rs = avg_gain / (avg_loss + 1e-12)
        return 100.0 - (100.0 / (1.0 + rs))
    if per_symbol:
        return _group(df).apply(lambda g: _rsi(g[col], window_bars))
    return _rsi(df[col], window_bars)

@register_op("ta.bbands")
def op_ta_bbands(df: pd.DataFrame, col: str = "close", window_bars: int = 20, k: float = 2.0,
                 band: str = "upper", per_symbol: bool = True, **_) -> pd.Series:
    def _bb(g: pd.Series):
        ma = g.rolling(window_bars, min_periods=1).mean()
        sd = g.rolling(window_bars, min_periods=1).std()
        upper = ma + k * sd
        lower = ma - k * sd
        mid = ma
        if band == "upper":
            return upper
        elif band == "lower":
            return lower
        return mid
    if per_symbol:
        return _group(df).apply(lambda gg: _bb(gg[col]))
    return _bb(df[col])

@register_op("ta.macd")
def op_ta_macd(df: pd.DataFrame, col: str = "close", fast: int = 12, slow: int = 26, signal: int = 9,
               per_symbol: bool = True, **_) -> pd.DataFrame:
    def _macd(s: pd.Series):
        ema_fast = _ema(s, fast)
        ema_slow = _ema(s, slow)
        macd = ema_fast - ema_slow
        sig = _ema(macd, signal)
        hist = macd - sig
        out = pd.DataFrame({"macd": macd, "macd_signal": sig, "macd_hist": hist})
        return out
    if per_symbol:
        return _group(df).apply(lambda g: _macd(g[col])).reset_index(drop=True)
    return _macd(df[col])

@register_op("ta.atr")
def op_ta_atr(df: pd.DataFrame, window_bars: int = 14, per_symbol: bool = True, **_) -> pd.Series:
    """Welles Wilder ATR（机构标准）"""
    def _atr(g: pd.DataFrame):
        high, low, close = g["high"], g["low"], g["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        # Wilder's smoothing: ewm(alpha=1/n, adjust=False)
        return tr.ewm(alpha=1.0 / float(window_bars), adjust=False, min_periods=1).mean()
    if per_symbol:
        return _group(df).apply(_atr)
    return _atr(df)

@register_op("ta.obv")
def op_ta_obv(df: pd.DataFrame, price_col: str = "close", vol_col: str = "volume", per_symbol: bool = True, **_) -> pd.Series:
    def _obv(g: pd.DataFrame):
        sgn = np.sign(g[price_col].diff().fillna(0.0))
        return (sgn * g[vol_col]).cumsum()
    if per_symbol:
        return _group(df).apply(_obv)
    return _obv(df)

# 导出
OPS = _OP_REGISTRY
