# features/standard_features.py

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

# AI 大脑可以使用的 "Python 衍生特征" 名单
PYTHON_DERIVED_FEATURES: List[str] = [
    # 返回收益类
    "ret_1b",
    "ret_5b",
    "ret_10b",

    # 波动类
    "vol_20b",          # 20 bar rolling std of returns

    # 均线/趋势类
    "ma_20_close",
    "ma_50_close",
    "ma_200_close",

    # 量能 / RVOL
    "vol_ma_20",
    "rvol_20",

    # 时间位置类（可以辅助模型感知开盘/收盘时段）
    "minute_of_day",
    "day_of_week",
]


def _ensure_datetime(df: pd.DataFrame, ts_col: str) -> pd.DataFrame:
    if ts_col not in df.columns:
        return df

    if not np.issubdtype(df[ts_col].dtype, np.datetime64):
        df = df.copy()
        df[ts_col] = pd.to_datetime(df[ts_col])
    return df


def add_standard_features(
    df: pd.DataFrame,
    *,
    enabled: Optional[Sequence[str]] = None,
    group_key: Sequence[str] = ("symbol",),
    ts_col: str = "ts",
) -> pd.DataFrame:
    """
    在 df 上增加一批"标准衍生特征"。

    约束:
      - 所有特征都只使用过去或当前 bar（无未来泄露）
      - 按 group_key 分组（一般按 symbol 分组）
    """
    if enabled is None:
        enabled = PYTHON_DERIVED_FEATURES

    enabled = list(enabled)
    if not enabled:
        return df

    needed_price_cols = {"close"}
    needed_vol_cols = {"volume"}

    # 缺基础列的特征自动跳过
    missing_price = needed_price_cols - set(df.columns)
    missing_vol = needed_vol_cols - set(df.columns)

    df = _ensure_datetime(df, ts_col)
    out = df.copy()

    # 分组按 symbol（或你指定的 group_key）
    grp = out.groupby(list(group_key), sort=False, group_keys=False)

    # === 1. 收益类特征 ===
    if "ret_1b" in enabled and "close" in out.columns:
        out["ret_1b"] = grp["close"].pct_change(1)

    if "ret_5b" in enabled and "close" in out.columns:
        out["ret_5b"] = grp["close"].pct_change(5)

    if "ret_10b" in enabled and "close" in out.columns:
        out["ret_10b"] = grp["close"].pct_change(10)

    # === 2. 波动类特征（滚动标准差） ===
    if "vol_20b" in enabled and "ret_1b" in out.columns:
        out["vol_20b"] = grp["ret_1b"].rolling(20, min_periods=5).std().reset_index(level=0, drop=True)

    # === 3. 均线 / 趋势类特征（简单移动平均） ===
    if "ma_20_close" in enabled and "close" in out.columns:
        out["ma_20_close"] = grp["close"].rolling(20, min_periods=5).mean().reset_index(level=0, drop=True)

    if "ma_50_close" in enabled and "close" in out.columns:
        out["ma_50_close"] = grp["close"].rolling(50, min_periods=10).mean().reset_index(level=0, drop=True)

    if "ma_200_close" in enabled and "close" in out.columns:
        out["ma_200_close"] = grp["close"].rolling(200, min_periods=20).mean().reset_index(level=0, drop=True)

    # === 4. 成交量 / RVOL 类特征 ===
    if "vol_ma_20" in enabled and "volume" in out.columns:
        out["vol_ma_20"] = grp["volume"].rolling(20, min_periods=5).mean().reset_index(level=0, drop=True)

    if (
        "rvol_20" in enabled
        and "volume" in out.columns
        and "vol_ma_20" in out.columns
    ):
        # RVOL = volume / vol_ma_20
        out["rvol_20"] = out["volume"] / out["vol_ma_20"]

    # === 5. 时间位置类特征 ===
    if "minute_of_day" in enabled and ts_col in out.columns:
        out["minute_of_day"] = out[ts_col].dt.hour * 60 + out[ts_col].dt.minute

    if "day_of_week" in enabled and ts_col in out.columns:
        # 周一=0, 周日=6
        out["day_of_week"] = out[ts_col].dt.dayofweek

    return out
