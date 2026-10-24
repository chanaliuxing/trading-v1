# label_factory.py
# SPDX-License-Identifier: Apache-2.0
#
# 标签 DSL：只改 JSON 就能切换"日内/隔夜/分类/回归/分位数"
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple, List, Optional

def _pct(a, b):
    """计算百分比收益: (b/a - 1)"""
    return (b / np.maximum(a, 1e-12)) - 1.0

def _winsorize(s: pd.Series, p_low=0.01, p_high=0.99):
    """Winsorize处理：截断极端值"""
    lo, hi = s.quantile(p_low), s.quantile(p_high)
    return s.clip(lower=lo, upper=hi)

def _zclip(s: pd.Series, z=5.0):
    """Z-score clip：截断超过z个标准差的值"""
    m, sd = s.mean(), s.std(ddof=1)
    if sd <= 1e-12: return s
    return s.clip(lower=m - z*sd, upper=m + z*sd)

def build_label(df: pd.DataFrame, spec: Dict[str, Any]) -> Tuple[pd.Series, Dict[str, Any]]:
    """
    统一标签生成入口

    Args:
        df: 包含OHLCV的DataFrame
        spec: 标签配置字典，支持以下类型：

            **forward_return**: 前向收益率
            {
              "type": "forward_return",
              "horizon_bars": 30,          # 前向周期
              "price_from": "close|open",  # 起始价格
              "price_to": "close|open|next_open",  # 目标价格
              "winsorize": {"p_low":0.01,"p_high":0.99},  # 可选
              "zclip": 5.0,                # 可选
              "label_name": "future_ret_30m"
            }

            **bar_return**: Bar累计收益
            {
              "type": "bar_return",
              "horizon_bars": 30,
              ...
            }

            **overnight_return**: 隔夜收益（收盘→次日开盘）
            {
              "type": "overnight_return",
              "horizon_bars": 1,  # 1=单日，>1=多日累加
              ...
            }

            **binary_threshold**: 二分类（基于收益阈值）
            {
              "type": "binary_threshold",
              "threshold": 0.0,
              "base": {"type": "forward_return", "horizon_bars": 15},
              ...
            }

            **quantile_class**: 分位数分类
            {
              "type": "quantile_class",
              "quantiles": 5,
              "base": {"type": "forward_return", "horizon_bars": 30},
              ...
            }

    Returns:
        (label_series, meta_dict): 标签序列和元数据
    """
    sname = spec.get("label_name", "label")
    stype = spec.get("type", "forward_return")
    sess  = spec.get("session", "ALL")
    price_from = spec.get("price_from", "close")
    price_to   = spec.get("price_to", "close")
    hz = int(spec.get("horizon_bars", 30))

    use = df.copy()  # 若需要可在此处过滤 RTH

    # 价格列
    open_ = use["open"].astype(float)
    close = use["close"].astype(float)

    if stype == "forward_return":
        # close->close, open->close, close->open 都可通过 price_from/price_to 组合
        src = {"open": open_, "close": close}[price_from]
        if price_to == "next_open":
            tgt = open_.shift(-hz)
        else:
            tgt = {"open": open_, "close": close}[price_to].shift(-hz)
        label = _pct(src, tgt)

    elif stype == "bar_return":
        # 单bar 累计 (rolling sum of per-bar ret) 的近似
        per_bar = _pct(close, close.shift(-1))
        label = per_bar.rolling(window=hz, min_periods=hz).sum().shift(-(hz-1))

    elif stype == "overnight_return":
        # 收盘->次日开盘
        tgt = open_.shift(-1)
        label = _pct(close, tgt)
        if hz > 1:
            # 多日隔夜累加
            label = label.rolling(window=hz, min_periods=hz).sum().shift(-(hz-1))

    elif stype == "binary_threshold":
        base = spec.get("base", {"type": "forward_return", "horizon_bars": hz})
        base["label_name"] = sname + "_raw"
        raw, _ = build_label(df, base)
        thr = float(spec.get("threshold", 0.0))
        label = (raw > thr).astype(int)

    elif stype == "quantile_class":
        base = spec.get("base", {"type": "forward_return", "horizon_bars": hz})
        base["label_name"] = sname + "_raw"
        raw, _ = build_label(df, base)
        q = int(spec.get("quantiles", 5))
        label = pd.qcut(raw, q=q, labels=False, duplicates="drop")

    else:
        raise ValueError(f"Unknown label type: {stype}")

    # 清洗
    if "winsorize" in spec and isinstance(label, pd.Series) and label.dtype != int:
        label = _winsorize(label, **spec["winsorize"])
    if "zclip" in spec and isinstance(label, pd.Series) and label.dtype != int:
        label = _zclip(label, z=float(spec["zclip"]))

    label.name = sname
    meta = {"label_name": sname, "type": stype, "horizon_bars": hz, "session": sess}
    return label, meta
