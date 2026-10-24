# risk_metrics.py
# SPDX-License-Identifier: Apache-2.0
#
# 机构口径：年化、Sortino、PF、MDD、期望值
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional, Dict

_EPS = 1e-12

def _np(a): return np.asarray(a, dtype=float)

def annualize_factor(bar_secs: int) -> float:
    """
    计算年化因子
    美股 RTH ~6.5h/天，252 交易日
    更稳健的方式是用实际频率估计
    """
    # 每年秒数 / bar秒数 * RTH占比（~ 0.27）
    year_secs = 365 * 24 * 3600
    return max(year_secs / max(bar_secs, 1) * 0.27, 1.0)

def sharpe_ratio(returns: np.ndarray, bar_secs: int, rf: float = 0.0) -> float:
    """
    年化Sharpe比率

    Args:
        returns: 收益序列
        bar_secs: bar周期（秒）
        rf: 无风险利率

    Returns:
        年化Sharpe比率
    """
    r = _np(returns) - rf
    m, s = r.mean(), r.std(ddof=1)
    if s < _EPS: return 0.0
    return float(m / s * np.sqrt(annualize_factor(bar_secs)))

def sortino_ratio(returns: np.ndarray, bar_secs: int, rf: float = 0.0) -> float:
    """
    年化Sortino比率（仅考虑下行波动）

    Args:
        returns: 收益序列
        bar_secs: bar周期（秒）
        rf: 无风险利率

    Returns:
        年化Sortino比率
    """
    r = _np(returns) - rf
    neg = r[r < 0.0]
    ds = neg.std(ddof=1) if neg.size > 1 else 0.0
    if ds < _EPS: return 0.0
    return float(r.mean() / ds * np.sqrt(annualize_factor(bar_secs)))

def max_drawdown(equity_curve: np.ndarray) -> float:
    """
    最大回撤（负数表示回撤）

    Args:
        equity_curve: 权益曲线

    Returns:
        最大回撤（<= 0）
    """
    ec = _np(equity_curve)
    peak = np.maximum.accumulate(ec)
    dd = (ec - peak) / np.maximum(peak, _EPS)
    return float(dd.min())

def profit_factor(returns: np.ndarray) -> float:
    """
    盈利因子（总盈利/总亏损）

    Args:
        returns: 收益序列

    Returns:
        盈利因子（>1表示盈利）
    """
    r = _np(returns)
    gp = r[r > 0.0].sum()
    gl = -r[r < 0.0].sum()
    if gl < _EPS: return float('inf') if gp > 0 else 0.0
    return float(gp / gl)

def expectancy_per_trade(returns: np.ndarray) -> float:
    """
    每笔交易期望值

    Args:
        returns: 收益序列

    Returns:
        平均收益
    """
    r = _np(returns)
    if r.size == 0: return 0.0
    return float(r.mean())

def summarize_oos(returns: np.ndarray, bar_secs: int) -> Dict[str, float]:
    """
    OOS性能汇总（机构标准）

    Args:
        returns: OOS每笔/每bar PnL比例或对数收益（保持口径一致）
        bar_secs: bar周期（秒），用于年化计算

    Returns:
        包含以下字段的字典：
        - oos_avg_sharpe: 年化Sharpe
        - oos_sortino: 年化Sortino
        - pf: 盈利因子
        - expectancy: 每笔期望值
        - max_dd: 最大回撤
        - trades: 交易笔数
    """
    sr = sharpe_ratio(returns, bar_secs)
    so = sortino_ratio(returns, bar_secs)
    pf = profit_factor(returns)
    exp = expectancy_per_trade(returns)

    # 构造权益曲线估计 MDD（假设简单累加）
    eq = np.cumsum(returns)
    mdd = max_drawdown(eq)

    return {
        "oos_avg_sharpe": float(sr),
        "oos_sortino": float(so),
        "pf": float(pf),
        "expectancy": float(exp),
        "max_dd": float(mdd),
        "trades": int(returns.size),
    }
