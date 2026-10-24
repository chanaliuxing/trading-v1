# ic_metrics.py
# SPDX-License-Identifier: Apache-2.0
#
# 稳健 Spearman-IC + 覆盖率（横截面平均 IC 和时间序列 IC）
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Tuple, Optional, Literal

Mode = Literal["cross_sectional", "timeseries"]

def _safe_rank(a: np.ndarray) -> np.ndarray:
    """
    平均秩处理（ties-average），NaN 保持 NaN。返回 float32。
    """
    x = a.astype(np.float64, copy=False)
    n = x.size
    idx = np.arange(n)
    mask = ~np.isfinite(x)
    x2 = x.copy()
    x2[mask] = np.nan
    order = np.argsort(x2, kind="mergesort")  # 稳定排序
    ranks = np.empty(n, dtype=np.float64)
    ranks.fill(np.nan)

    # 遍历相等段落（ties）
    i = 0
    while i < n:
        j = i
        xi = x2[order[i]]
        if not np.isfinite(xi):
            # 后面全是 NaN
            break
        while j + 1 < n and x2[order[j + 1]] == xi:
            j += 1
        # 平均秩 = (i + j) / 2 + 1（秩从1开始）
        avg_rank = (i + j) * 0.5 + 1.0
        ranks[order[i:j + 1]] = avg_rank
        i = j + 1

    return ranks.astype(np.float32, copy=False)

def _spearmanr_1d(y_true: np.ndarray, y_pred: np.ndarray) -> Optional[float]:
    """
    无 SciPy 版本的 Spearman 相关。若有效样本 < 3，返回 None。
    """
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    if mask.sum() < 3:
        return None
    r1 = _safe_rank(y_true[mask])
    r2 = _safe_rank(y_pred[mask])
    # 普通皮尔逊相关
    r1m = r1.mean()
    r2m = r2.mean()
    num = np.sum((r1 - r1m) * (r2 - r2m))
    den = np.sqrt(np.sum((r1 - r1m) ** 2) * np.sum((r2 - r2m) ** 2))
    if den <= 0:
        return None
    return float(num / den)

def compute_ic_cross_sectional(df: pd.DataFrame,
                               ret_col: str,
                               pred_col: str,
                               ts_col: str = "ts",
                               min_n: int = 3,
                               weighted: bool = True) -> Tuple[Optional[float], float]:
    """
    横截面 IC：按时间戳聚合对 (ret, pred) 做 Spearman，再对天（或bar）平均。
    返回: (avg_ic, coverage)；coverage=有IC的切片数 / 全部切片数。
    """
    if ts_col not in df.columns:
        # 没有 ts，就退化成 timeseries
        return compute_ic_timeseries(df, ret_col, pred_col)
    grp = df.groupby(ts_col, sort=True, observed=True)
    ics, weights = [], []
    total = 0
    for t, g in grp:
        total += 1
        y = g[ret_col].to_numpy()
        p = g[pred_col].to_numpy()
        mask = np.isfinite(y) & np.isfinite(p)
        if mask.sum() < min_n:
            continue
        ic = _spearmanr_1d(y[mask], p[mask])
        if ic is None:
            continue
        ics.append(ic)
        weights.append(mask.sum())
    if not ics:
        return None, 0.0
    if weighted:
        w = np.asarray(weights, dtype=np.float64)
        ic = float(np.average(np.asarray(ics, dtype=np.float64), weights=w))
    else:
        ic = float(np.mean(np.asarray(ics, dtype=np.float64)))
    coverage = float(len(ics) / max(total, 1))
    return ic, coverage

def compute_ic_timeseries(df: pd.DataFrame,
                          ret_col: str,
                          pred_col: str,
                          symbol_col: Optional[str] = "symbol",
                          min_n: int = 3) -> Tuple[Optional[float], float]:
    """
    时间序列 IC：对单序列（或拼接多 symbol）整体做 Spearman。
    因子检验里没有横截面时可使用。
    """
    if symbol_col and symbol_col in df.columns:
        # 拼接多 symbol —— 与其做分组平均，不如整体排名（更稳健）
        y = df[ret_col].to_numpy()
        p = df[pred_col].to_numpy()
        ic = _spearmanr_1d(y, p)
        cov = float(np.isfinite(y).sum() >= min_n and np.isfinite(p).sum() >= min_n)
        return ic, cov
    else:
        y = df[ret_col].to_numpy()
        p = df[pred_col].to_numpy()
        ic = _spearmanr_1d(y, p)
        cov = float(np.isfinite(y).sum() >= min_n and np.isfinite(p).sum() >= min_n)
        return ic, cov

def compute_ic_oos(df_oos: pd.DataFrame,
                   ret_col: str,
                   pred_col: str,
                   mode: Mode = "cross_sectional",
                   ts_col: str = "ts",
                   symbol_col: str = "symbol",
                   min_n: int = 3) -> Tuple[Optional[float], float]:
    """
    统一入口：OOS IC & 覆盖率。

    Args:
        df_oos: OOS样本DataFrame
        ret_col: 真实收益列名
        pred_col: 预测值列名
        mode: "cross_sectional" (多标的横截面) 或 "timeseries" (时间序列)
        ts_col: 时间戳列名（横截面模式用）
        symbol_col: 标的列名（时间序列模式用）
        min_n: 最小样本数

    Returns:
        (avg_ic, coverage): 平均IC值（None表示无法计算）和覆盖率（0-1）
    """
    if mode == "cross_sectional":
        return compute_ic_cross_sectional(df_oos, ret_col, pred_col, ts_col=ts_col, min_n=min_n)
    else:
        return compute_ic_timeseries(df_oos, ret_col, pred_col, symbol_col=symbol_col, min_n=min_n)
