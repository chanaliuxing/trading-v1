#!/usr/bin/env python3
# leakage_guard.py
#
# 目标：在 Walk-Forward / HPO 之前或之后，对数据切分和标签进行结构性检查，
#      尽可能发现「未来泄漏」「IS/OOS 重叠」「时间错乱」等致命问题。

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import numpy as np


@dataclass
class LeakageIssue:
    check: str            # 检查名称，如 "time_monotonic", "overlap_keys"
    level: str            # "INFO" / "WARN" / "ERROR"
    message: str
    stats: Dict[str, Any]


@dataclass
class LeakageReport:
    ok: bool
    issues: List[LeakageIssue]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "issues": [asdict(i) for i in self.issues],
        }


class LeakageGuard:
    """
    轻量级防泄漏组件，完全 DataFrame 驱动。

    主要检查：
    1) 全局时间单调性（按 symbol 分组）
    2) train / val 之间是否有重复 key（symbol, ts）
    3) train / val 的时间顺序是否满足「train 在前，val 在后」
    4) label 尾部 horizon_bars 行是否为 NaN（否则强烈怀疑用了未来数据）
    """

    def __init__(
        self,
        *,
        time_col: str = "ts",
        symbol_col: str = "symbol",
        label_col: Optional[str] = None,
        label_horizon_bars: Optional[int] = None,
        # 可选阈值配置
        max_overlap_ratio: float = 0.0,   # 允许 train/val key 重叠比例（0 表示不允许）
        strict_time_order: bool = True,   # 若 True，要求 per symbol 上 val_min_ts > train_max_ts
    ) -> None:
        self.time_col = time_col
        self.symbol_col = symbol_col
        self.label_col = label_col
        self.label_horizon_bars = label_horizon_bars
        self.max_overlap_ratio = max_overlap_ratio
        self.strict_time_order = strict_time_order

    # ----------------- 公共入口 -----------------

    def run_all(
        self,
        df: pd.DataFrame,
        train_idx: Sequence[int],
        val_idx: Sequence[int],
    ) -> LeakageReport:
        issues: List[LeakageIssue] = []

        # 1) 基本列检查
        issues.extend(self._check_basic_columns(df))

        # 2) 时间单调性检查
        issues.extend(self._check_time_monotonic(df))

        # 3) 切分重叠检查
        issues.extend(self._check_overlap(df, train_idx, val_idx))

        # 4) 时间先后检查
        issues.extend(self._check_time_order(df, train_idx, val_idx))

        # 5) Label 尾部 NaN 检查（依赖 horizon）
        if self.label_col and self.label_horizon_bars and self.label_horizon_bars > 0:
            issues.extend(self._check_label_tail(df))

        # 评估整体 ok 与否
        ok = True
        for iss in issues:
            if iss.level == "ERROR":
                ok = False
                break

        return LeakageReport(ok=ok, issues=issues)

    # ----------------- 具体检查 -----------------

    def _check_basic_columns(self, df: pd.DataFrame) -> List[LeakageIssue]:
        issues: List[LeakageIssue] = []
        missing = []

        for col in [self.time_col, self.symbol_col]:
            if col not in df.columns:
                missing.append(col)

        if missing:
            issues.append(
                LeakageIssue(
                    check="basic_columns",
                    level="WARN",
                    message=f"缺少基础列: {missing}，部分泄漏检查将被跳过。",
                    stats={"missing": missing},
                )
            )
        return issues

    def _check_time_monotonic(self, df: pd.DataFrame) -> List[LeakageIssue]:
        issues: List[LeakageIssue] = []
        if self.time_col not in df.columns or self.symbol_col not in df.columns:
            # 已在 basic_columns 标记
            return issues

        # 按 symbol 分组后检查时间是否单调不减（允许等于）
        df = df.copy()
        df[self.time_col] = pd.to_datetime(df[self.time_col])

        gb = df.sort_values([self.symbol_col, self.time_col]).groupby(self.symbol_col)
        broken_symbols: List[str] = []
        for sym, g in gb:
            t = g[self.time_col].values
            if len(t) <= 1:
                continue
            # 如果存在 t[i] > t[i+1] 则时间逆序
            if np.any(t[1:] < t[:-1]):
                broken_symbols.append(str(sym))

        if broken_symbols:
            issues.append(
                LeakageIssue(
                    check="time_monotonic",
                    level="ERROR",
                    message="部分 symbol 时间戳非单调递增，可能存在数据错位或重复。",
                    stats={"symbols": broken_symbols},
                )
            )
        else:
            issues.append(
                LeakageIssue(
                    check="time_monotonic",
                    level="INFO",
                    message="时间戳在每个 symbol 内单调递增。",
                    stats={},
                )
            )
        return issues

    def _make_key_series(self, df: pd.DataFrame) -> pd.Series:
        if self.time_col not in df.columns:
            return pd.Series(index=df.index, data=np.arange(len(df)), name="_row_id")
        if self.symbol_col not in df.columns:
            return df[self.time_col].astype("int64")
        # tuple (symbol, ts_int)
        t = pd.to_datetime(df[self.time_col]).astype("int64")
        s = df[self.symbol_col].astype("string")
        return s.str.cat(t.astype(str), sep="|")

    def _check_overlap(
        self,
        df: pd.DataFrame,
        train_idx: Sequence[int],
        val_idx: Sequence[int],
    ) -> List[LeakageIssue]:
        issues: List[LeakageIssue] = []

        train = df.loc[train_idx]
        val = df.loc[val_idx]

        k_train = self._make_key_series(train)
        k_val = self._make_key_series(val)

        # 使用 set 交集
        set_tr = set(k_train.tolist())
        set_va = set(k_val.tolist())

        inter = set_tr & set_va
        if not inter:
            issues.append(
                LeakageIssue(
                    check="overlap_keys",
                    level="INFO",
                    message="train / val 没有发现重复 key（按 symbol+ts）。",
                    stats={},
                )
            )
            return issues

        # 有重叠
        overlap_cnt = len(inter)
        min_size = min(len(set_tr), len(set_va)) or 1
        ratio = overlap_cnt / float(min_size)

        lvl = "ERROR" if ratio > self.max_overlap_ratio else "WARN"

        issues.append(
            LeakageIssue(
                check="overlap_keys",
                level=lvl,
                message=(
                    f"train / val 之间发现 {overlap_cnt} 个重复 key，"
                    f"重叠比例约 {ratio:.4f}（相对于较小集合）。"
                ),
                stats={
                    "overlap_count": overlap_cnt,
                    "overlap_ratio": ratio,
                    "max_allowed_ratio": self.max_overlap_ratio,
                },
            )
        )
        return issues

    def _check_time_order(
        self,
        df: pd.DataFrame,
        train_idx: Sequence[int],
        val_idx: Sequence[int],
    ) -> List[LeakageIssue]:
        issues: List[LeakageIssue] = []

        if self.time_col not in df.columns or self.symbol_col not in df.columns:
            return issues

        df = df.copy()
        df[self.time_col] = pd.to_datetime(df[self.time_col])

        train = df.loc[train_idx]
        val = df.loc[val_idx]

        # 对每个 symbol，检查 val 最小时间是否严格大于 train 最大时间
        train_max = train.groupby(self.symbol_col)[self.time_col].max()
        val_min = val.groupby(self.symbol_col)[self.time_col].min()

        bad_symbols: List[Tuple[str, str, str]] = []
        for sym, tmax in train_max.items():
            if sym not in val_min:
                continue
            tmin = val_min[sym]
            if tmin <= tmax:
                bad_symbols.append(
                    (str(sym), str(tmax), str(tmin))
                )

        if not bad_symbols:
            issues.append(
                LeakageIssue(
                    check="time_order",
                    level="INFO",
                    message="train 在前，val 在后（按 symbol 时间边界检查）。",
                    stats={},
                )
            )
            return issues

        lvl = "ERROR" if self.strict_time_order else "WARN"
        issues.append(
            LeakageIssue(
                check="time_order",
                level=lvl,
                message=(
                    f"发现 {len(bad_symbols)} 个 symbol 的 val_min_ts <= train_max_ts，"
                    f"可能不是严格滚动窗口，或存在样本交叉。"
                ),
                stats={"examples": bad_symbols[:10]},
            )
        )
        return issues

    def _check_label_tail(self, df: pd.DataFrame) -> List[LeakageIssue]:
        issues: List[LeakageIssue] = []
        if self.label_col not in df.columns:
            issues.append(
                LeakageIssue(
                    check="label_tail",
                    level="WARN",
                    message=f"label 列 {self.label_col!r} 不存在，跳过尾部 NaN 检查。",
                    stats={},
                )
            )
            return issues

        if self.label_horizon_bars is None or self.label_horizon_bars <= 0:
            return issues

        df = df.copy()
        df = df.sort_values([self.symbol_col, self.time_col])
        gb = df.groupby(self.symbol_col)

        bad_symbols: List[str] = []
        for sym, g in gb:
            tail = g[self.label_col].tail(self.label_horizon_bars)
            # 理想情况下，这些尾部应该因为「未来不存在」而为 NaN
            if tail.notna().any():
                bad_symbols.append(str(sym))

        if bad_symbols:
            issues.append(
                LeakageIssue(
                    check="label_tail",
                    level="WARN",
                    message=(
                        f"部分 symbol 尾部 {self.label_horizon_bars} 条的 label 非 NaN，"
                        "如果 label 是未来收益，这强烈暗示使用了未来数据或提前截断时未清理尾部样本。"
                    ),
                    stats={"symbols": bad_symbols},
                )
            )
        else:
            issues.append(
                LeakageIssue(
                    check="label_tail",
                    level="INFO",
                    message=(
                        f"每个 symbol 尾部 {self.label_horizon_bars} 条 label 均为 NaN，"
                        "与「使用未来信息构造标签」的预期一致（已正确截断）。"
                    ),
                    stats={},
                )
            )
        return issues


# ================================================================================
# Point-in-Time Data Filter
# ================================================================================

def enforce_point_in_time(
    df: pd.DataFrame,
    ts_col: str = "ts",
    cutoff_col: str = "feature_cutoff_ts",
) -> pd.DataFrame:
    """
    Point-in-time leakage guard: Filter out rows where ts < cutoff_col.

    This ensures that features are only used if they were available at the bar time.
    If cutoff_col doesn't exist, returns df unchanged.

    Args:
        df: DataFrame with time-series data
        ts_col: Name of the timestamp column
        cutoff_col: Name of the cutoff timestamp column

    Returns:
        Filtered DataFrame where ts >= cutoff_col for all rows
    """
    if cutoff_col not in df.columns:
        return df

    if ts_col not in df.columns:
        return df

    # Ensure both columns are datetime
    if not pd.api.types.is_datetime64_any_dtype(df[ts_col]):
        df = df.copy()
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)

    if not pd.api.types.is_datetime64_any_dtype(df[cutoff_col]):
        df = df.copy()
        df[cutoff_col] = pd.to_datetime(df[cutoff_col], utc=True)

    # Filter: keep only rows where ts >= cutoff_col
    before = len(df)
    df = df[df[ts_col] >= df[cutoff_col]].copy()
    after = len(df)

    if before > after:
        dropped = before - after
        print(f"[enforce_point_in_time] Dropped {dropped} rows ({100*dropped/before:.1f}%) where {ts_col} < {cutoff_col}")

    return df


def apply_embargo_to_splits(
    splits: Iterable[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]],
    embargo: pd.Timedelta,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """
    Apply an embargo gap between IS and OOS periods.

    Each split is a tuple: (is_start, is_end, oos_start, oos_end)
    We shift the OOS start forward by `embargo` and leave OOS end unchanged.
    If the embargo would invert the window (oos_start >= oos_end), the split is dropped.

    This is a lightweight, time-only version used by wf_hpo.make_folds.
    """
    out: List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
    if embargo is None or embargo <= pd.Timedelta(0):
        return list(splits)
    for is_start, is_end, oos_start, oos_end in splits:
        try:
            new_oos_start = oos_start + embargo
        except Exception:
            # Fallback: if embargo application fails, keep original split
            out.append((is_start, is_end, oos_start, oos_end))
            continue
        # Drop degenerate splits where embargo wipes out OOS
        if new_oos_start >= oos_end:
            continue
        out.append((is_start, is_end, new_oos_start, oos_end))
    return out
