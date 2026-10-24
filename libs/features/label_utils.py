# label_utils.py (calendar-aware)
# SPDX-License-Identifier: Apache-2.0
#
# 交易日历感知的标签生成：支持严格对齐/交易时段推进/锚点跳跃三种模式
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Optional
from libs.features.market_calendar_utils import (
    build_calendar_from_config, advance_in_sessions, jump_to_session_anchor, ET
)


def _forward_match_strict(
    df: pd.DataFrame,
    horizon_sec: int,
    *,
    ts_col="ts",
    symbol_col="symbol",
    to_col="close",
    max_slip_sec: int = 0
) -> pd.Series:
    """
    严格模式：merge_asof精确对齐

    Args:
        df: 数据DataFrame
        horizon_sec: 向前偏移秒数
        ts_col: 时间戳列名
        symbol_col: 标的列名
        to_col: 目标价格列名
        max_slip_sec: 最大滑点容忍秒数

    Returns:
        未来价格Series（可能含NaN）
    """
    # Create a DataFrame for the left side of merge_asof
    # This DataFrame should contain the original index of 'df'
    left_df = df[[symbol_col, ts_col, to_col]].copy()
    left_df['original_index'] = df.index # Add original index as a column
    left_df = left_df.sort_values([symbol_col, ts_col])

    # Prepare 'left' for merge_asof
    left = left_df.rename(columns={to_col: "from_val"})
    left["ts_query"] = left[ts_col] + pd.to_timedelta(int(horizon_sec), unit="s")

    # Prepare 'right' for merge_asof
    right_df = df[[symbol_col, ts_col, to_col]].copy()
    right_df = right_df.sort_values([symbol_col, ts_col])
    right = right_df.rename(columns={to_col: "to_val"})

    kwargs = dict(
        left_on="ts_query",
        right_on=ts_col,
        by=symbol_col,
        direction="forward"
    )

    if max_slip_sec >= 0:
        kwargs["tolerance"] = pd.Timedelta(seconds=int(max_slip_sec))

    merged = pd.merge_asof(
        left.sort_values(["ts_query"]),
        right.sort_values([ts_col]),
        **kwargs
    )

    # Now, 'merged' should contain 'original_index' from 'left'
    # We need to map 'to_val' back to the original 'df' index using 'original_index'

    px_to_series = merged.set_index('original_index')['to_val'].reindex(df.index)

    return px_to_series


def _forward_match_sessions(
    df: pd.DataFrame,
    horizon_sec: int,
    *,
    ts_col="ts",
    symbol_col="symbol",
    to_col="close",
    cal_cfg: Dict
) -> pd.Series:
    """
    交易时段模式：沿交易时段推进horizon_sec

    跨日会自动滚入下一交易日开盘继续推进

    Args:
        df: 数据DataFrame
        horizon_sec: 向前推进秒数
        ts_col: 时间戳列名
        symbol_col: 标的列名
        to_col: 目标价格列名
        cal_cfg: config.yml配置字典

    Returns:
        未来价格Series（可能含NaN）
    """
    cal = build_calendar_from_config(cal_cfg)

    # 逐symbol处理（避免groupby.apply反复构建日历）
    out = pd.Series(index=df.index, dtype="float64")

    for sym, g in df.groupby(symbol_col, sort=False):
        # 确保时区为ET
        ts_et = g[ts_col].dt.tz_localize(None).dt.tz_localize(ET).astype("datetime64[ns]") \
                if g[ts_col].dt.tz is None else g[ts_col].dt.tz_convert(ET)

        targets = []
        for t in ts_et:
            t_next = advance_in_sessions(pd.Timestamp(t), horizon_sec, cal)
            targets.append(t_next)

        # 将targets回合到g的时间轴上（严格：要求存在完全一致的timestamp）
        target_ts = pd.Series(targets, index=g.index)

        # 精确匹配：inner join
        g2 = g.set_index(ts_col)
        px_map = g2[to_col].copy()
        px_map.index = px_map.index.tz_convert(ET) if px_map.index.tz else px_map.index.tz_localize(ET)

        val = px_map.reindex(target_ts.values)
        out.loc[g.index] = val.values

    return out


def _forward_match_anchor(
    df: pd.DataFrame,
    anchor: str,
    *,
    ts_col="ts",
    symbol_col="symbol",
    to_col="open",
    cal_cfg: Dict
) -> pd.Series:
    """
    锚点模式：直接跳到交易时段锚点（next_open / same_day_close）

    常用于隔夜开盘/收盘标签

    Args:
        df: 数据DataFrame
        anchor: 锚点类型（"next_open" | "same_day_close"）
        ts_col: 时间戳列名
        symbol_col: 标的列名
        to_col: 目标价格列名
        cal_cfg: config.yml配置字典

    Returns:
        锚点价格Series（可能含NaN）
    """
    cal = build_calendar_from_config(cal_cfg)

    out = pd.Series(index=df.index, dtype="float64")

    for sym, g in df.groupby(symbol_col, sort=False):
        # 确保时区为ET
        ts_et = g[ts_col].dt.tz_localize(None).dt.tz_localize(ET).astype("datetime64[ns]") \
                if g[ts_col].dt.tz is None else g[ts_col].dt.tz_convert(ET)

        targets = [jump_to_session_anchor(pd.Timestamp(t), cal, anchor=anchor) for t in ts_et]

        # 精确匹配
        g2 = g.set_index(ts_col)
        px_map = g2[to_col].copy()
        px_map.index = px_map.index.tz_convert(ET) if px_map.index.tz else px_map.index.tz_localize(ET)

        val = px_map.reindex(targets)
        out.loc[g.index] = val.values

    return out


def compute_label(
    df: pd.DataFrame,
    policy: Dict,
    *,
    ts_col: str = "ts",
    symbol_col: str = "symbol",
    config_yaml: Optional[Dict] = None
) -> pd.Series:
    """
    从ml_policy.json生成y标签（支持交易日历感知）

    Args:
        df: 数据DataFrame
        policy: ml_policy.json配置字典
        ts_col: 时间戳列名
        symbol_col: 标的列名
        config_yaml: config.yml解析后的字典（可选）

    Returns:
        标签Series（可能含NaN）

    支持模式：
    - "strict": 严格merge_asof对齐（默认）
    - "sessions": 按交易时段推进horizon_sec
    - "anchor": 直接跳到锚点（next_open / same_day_close）

    policy关键字段：
      - task: "regression"|"binary"|"multiclass"
      - label: {
          horizon_sec: 未来偏移秒数,
          method: "logret"|"pctret"|"cls_bins"|"auto_bins",
          from_col: 起始价格列,
          to_col: 目标价格列,
          clip_abs: 截断阈值,
          max_slip_sec: 最大滑点容忍秒数 (仅strict模式),
          calendar_mode: "strict"|"sessions"|"anchor",
          anchor: "next_open"|"same_day_close" (仅anchor模式)
        }
    """
    lab = policy.get("label", {})
    task = policy.get("task", "regression")
    hz   = int(lab.get("horizon_sec", 60))
    method = (lab.get("method") or "logret").lower()
    from_col = lab.get("from_col", "close")
    to_col   = lab.get("to_col",   "close")
    max_slip = int(lab.get("max_slip_sec", 0))

    # 日历模式
    calendar_mode = (lab.get("calendar_mode") or "strict").lower()   # "strict" | "sessions" | "anchor"
    anchor = (lab.get("anchor") or "next_open").lower()              # 对应anchor模式
    cal_cfg = (config_yaml or {})

    df = df.sort_values([symbol_col, ts_col]).copy()
    px_from = df[from_col].astype("float64")

    # 根据模式选择不同的对齐方法
    if calendar_mode == "sessions":
        px_to = _forward_match_sessions(
            df, hz,
            ts_col=ts_col,
            symbol_col=symbol_col,
            to_col=to_col,
            cal_cfg=cal_cfg
        )
    elif calendar_mode == "anchor":
        px_to = _forward_match_anchor(
            df, anchor,
            ts_col=ts_col,
            symbol_col=symbol_col,
            to_col=to_col,
            cal_cfg=cal_cfg
        )
    else:  # strict模式（默认）
        px_to = _forward_match_strict(
            df, hz,
            ts_col=ts_col,
            symbol_col=symbol_col,
            to_col=to_col,
            max_slip_sec=max_slip
        )

    # 计算收益率
    ret_log = np.log(px_to / px_from)
    ret_pct = (px_to / px_from) - 1.0
    base = ret_pct if method == "pctret" else ret_log

    # 截断极端值
    clip = lab.get("clip_abs", None)
    if clip is not None:
        base = base.clip(-abs(float(clip)), abs(float(clip)))

    # 任务分支
    if task == "binary":
        # 默认0为阈值；也可给定cls_bins=[-9,0,9]
        bins = lab.get("cls_bins", [-9, 0.0, 9])
        y = pd.cut(base, bins=bins, labels=[0, 0, 1], ordered=False).astype("float64")
    elif task == "multiclass":
        bins = lab.get("cls_bins", None)
        if bins is None or (isinstance(bins, str) and bins == "auto_bins"):
            # 按分位数自动等频分箱（3档示例，可改档数）
            q = base.quantile([0.33, 0.67]).values
            bins = [-np.inf, float(q[0]), float(q[1]), np.inf]
        y = pd.cut(base, bins=bins, labels=False).astype("float64")
    else:
        y = base  # 回归

    return y


def make_sample_weight(df: pd.DataFrame, policy: Dict) -> Optional[pd.Series]:
    """
    样本权重策略：
      - "uniform": None
      - "by_volatility": 需有atr_14（或自身已有的atr列），权重∝ATR^power
      - "by_volume": 权重∝成交量

    Args:
        df: 包含特征数据的DataFrame
        policy: ml_policy.json配置字典

    Returns:
        样本权重Series，如果为uniform则返回None
    """
    wpol = policy.get("weights", {}) or {}
    scheme = (wpol.get("scheme", "uniform") or "uniform").lower()

    if scheme == "uniform":
        return None

    if scheme == "by_volatility":
        atr_col = "atr_14" if "atr_14" in df.columns else None
        if atr_col is None:
            return None
        p = float(wpol.get("power", 1.0))
        w = (df[atr_col].astype("float64") ** p).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return (w / (w.mean() + 1e-12)).clip(0, 10.0)

    if scheme == "by_volume" and "volume" in df.columns:
        v = df["volume"].astype("float64")
        return (v / (v.mean() + 1e-12)).clip(0, 10.0)

    return None


def xgb_params_from_policy(policy: Dict) -> Dict:
    """
    从ml_policy.json提取XGBoost参数

    Args:
        policy: ml_policy.json配置字典

    Returns:
        XGBoost参数字典
    """
    loss = policy.get("loss", {}) or {}
    params = {
        "objective": loss.get("xgb_objective", "reg:squarederror"),
        "eval_metric": loss.get("xgb_eval_metric", ["rmse"]),
    }

    # 兼容多分类
    if policy.get("task") == "multiclass":
        em = params["eval_metric"]
        if isinstance(em, str):
            em = [em]
        if "mlogloss" not in em:
            em.append("mlogloss")
        params["eval_metric"] = em

    return params
