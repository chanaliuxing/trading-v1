# contracts/labels.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pandas as pd


@dataclass
class LabelConfig:
    """
    Label 的"契约":
      - kind: 目前先支持 horizon_return（N bar 前瞻收益）
      - horizon_bars: 使用多少个 bar 做前瞻
      - price_col: 用哪一列价格算收益
      - group_key: 分组键（一般按 symbol 分组）
      - ts_col: 时间列
      - required_columns: 构造 label 必须要有的列
      - task_types: 这个 label 适用于哪些 task（reg / rank / cls 等）
    """
    id: str
    kind: str  # "horizon_return" 先支持这一种
    horizon_bars: int
    price_col: str = "close"
    group_key: List[str] = field(default_factory=lambda: ["symbol"])
    ts_col: str = "ts"
    required_columns: List[str] = field(default_factory=lambda: ["symbol", "ts", "close"])
    task_types: List[str] = field(default_factory=lambda: ["reg"])


# ====== 内建一个基础 label: 10 bar 前瞻收益率 ======
LABEL_REGISTRY: Dict[str, LabelConfig] = {
    "ret_10b_fwd": LabelConfig(
        id="ret_10b_fwd",
        kind="horizon_return",
        horizon_bars=10,          # 10 个 bar（假设 1m bar 就是 10 分钟）
        price_col="close",
        group_key=["symbol"],
        ts_col="ts",
        required_columns=["symbol", "ts", "close"],
        task_types=["reg", "rank"],
    )
}


def get_label_config(label_id: str) -> LabelConfig:
    try:
        return LABEL_REGISTRY[label_id]
    except KeyError:
        raise KeyError(f"Unknown label_id '{label_id}'. Please register it in LABEL_REGISTRY.")


def build_label_series(df: pd.DataFrame, cfg: LabelConfig) -> pd.Series:
    """
    根据 LabelConfig 在 **Python 层** 构造 label —— 完全禁止 SQL 自己写 target。
    这里实现 horizon_return: fwd_close / close - 1
    """
    # 确保排序稳定，避免 shift 出错
    df_sorted = df.sort_values(cfg.group_key + [cfg.ts_col]).copy()

    grp = df_sorted.groupby(cfg.group_key, sort=False)
    fwd_price = grp[cfg.price_col].shift(-cfg.horizon_bars)
    current_price = df_sorted[cfg.price_col]

    label = fwd_price / current_price - 1.0

    # 再对齐回原 index，以免调用者混乱
    label = label.reindex(df.index)
    return label


def attach_label(
    df: pd.DataFrame,
    label_id: str,
    colname: str = "label",
) -> pd.DataFrame:
    """
    在 df 上增加一列 label，列名默认叫 'label'。
    """
    cfg = get_label_config(label_id)

    missing = [c for c in cfg.required_columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"DataFrame missing required columns for label '{label_id}': {missing}"
        )

    s = build_label_series(df, cfg)
    df_out = df.copy()
    df_out[colname] = s
    return df_out


def get_label_required_columns(label_id: str) -> List[str]:
    cfg = get_label_config(label_id)
    return list(cfg.required_columns)
