# features_injector.py
# SPDX-License-Identifier: Apache-2.0
#
# HPO注入/剪枝代码 - 零deepcopy、统一特征准备流程
from __future__ import annotations
from typing import Dict, Any, List, Tuple, Optional
import numpy as np
import pandas as pd

from libs.features.feature_dsl_v2 import build_features
from libs.features.feature_validator import validate_program

try:
    from sklearn.preprocessing import RobustScaler, StandardScaler
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False
    RobustScaler = None
    StandardScaler = None

# ---------- 1) Program 装载 + 构建 ----------
def apply_feature_program(df: pd.DataFrame,
                          programs: Dict[str, Any],
                          program_id: str,
                          allow_overwrite: bool = False) -> List[str]:
    """
    校验 + 执行特征程序（零拷贝）
    支持imports合并（如alpha_stack_v1）
    """
    # 校验
    validate_program(programs["programs"][program_id],
                    max_steps=programs["global"]["meta"]["max_steps"])

    # 执行主程序
    _, new_cols = build_features(df, programs["programs"], program_id, allow_overwrite=allow_overwrite)

    # 处理 imports 合并（如 alpha_stack_v1）
    prog = programs["programs"][program_id]
    if "imports" in prog:
        for pid in prog["imports"]:
            _, cols = build_features(df, programs["programs"], pid, allow_overwrite=False)
            new_cols += cols

    # 去重保持顺序
    return list(dict.fromkeys(new_cols))

# ---------- 2) 缺失/覆盖处理 ----------
def apply_nan_policy(df: pd.DataFrame, cols: List[str], policy: Dict[str, Any]) -> List[str]:
    """
    根据nan_policy过滤和填充特征
    """
    keep = []
    min_cov = float(policy.get("min_coverage", 0.85))
    fill = str(policy.get("fill", "ffill_then_zero"))

    for c in cols:
        if c not in df.columns:
            continue
        s = df[c]
        cov = float(s.notna().mean())
        if cov < min_cov:
            continue

        # 填充策略
        if fill == "zero":
            df[c] = s.fillna(0.0)
        elif fill == "mean":
            df[c] = s.fillna(s.mean())
        elif fill == "ffill":
            df[c] = s.ffill()
        elif fill == "ffill_then_zero":
            df[c] = s.ffill().fillna(0.0)
        elif fill == "none":
            pass  # 不填充

        keep.append(c)

    return keep

# ---------- 3) 标准化（Robust 优先） ----------
def standardize_block(df: pd.DataFrame,
                      cols: List[str],
                      std_meta: Dict[str, Any],
                      fit_mask: pd.Series) -> Tuple[List[str], Any]:
    """
    标准化特征块
    fit_mask: 布尔索引，True表示训练集
    """
    if not cols or not _HAS_SKLEARN:
        return cols, None

    scaler_name = std_meta.get("scaler", "robust")
    clip_sigma = float(std_meta.get("clip_sigma", 0.0))
    fit_on = std_meta.get("fit_on", "train_only")

    # 确定fit区间
    fit_idx = fit_mask if fit_on == "train_only" else df.index

    X_fit = df.loc[fit_idx, cols].to_numpy(dtype=np.float32)

    # 选择scaler
    if scaler_name == "robust":
        scaler = RobustScaler(with_centering=True, with_scaling=True, quantile_range=(25.0, 75.0))
    elif scaler_name == "zscore":
        scaler = StandardScaler()
    else:
        scaler = None

    if scaler is not None:
        scaler.fit(X_fit)
        X_all = df[cols].to_numpy(dtype=np.float32)
        X_all = scaler.transform(X_all)

        # Clip outliers
        if clip_sigma and clip_sigma > 0:
            X_all = np.clip(X_all, -clip_sigma, clip_sigma)

        # 就地更新
        df[cols] = X_all

    return cols, scaler

# ---------- 4) 低方差/强相关剪枝 ----------
def prune_by_variance_and_corr(df: pd.DataFrame,
                               cols: List[str],
                               var_floor: float,
                               corr_threshold: float) -> List[str]:
    """
    方差剪枝 + 相关性剪枝
    """
    if not cols:
        return cols

    # 方差剪枝
    keep = []
    data = df[cols].astype("float32")
    var = data.var(axis=0, ddof=1).to_numpy()

    for i, c in enumerate(cols):
        if float(var[i]) >= var_floor:
            keep.append(c)

    # 相关剪枝
    if len(keep) <= 1:
        return keep

    data2 = df[keep].astype("float32")
    corr = np.corrcoef(data2.T)
    n = len(keep)
    selected = []
    banned = set()

    for i in range(n):
        if i in banned:
            continue
        selected.append(keep[i])
        for j in range(i + 1, n):
            if abs(corr[i, j]) >= corr_threshold:
                banned.add(j)

    return selected

# ---------- 5) 模型重要性剪枝（可选） ----------
def prune_by_importance(importance_map: Dict[str, float],
                       cols: List[str],
                       floor: float,
                       topk: int) -> List[str]:
    """
    根据特征重要性排序和剪枝
    """
    ranked = sorted([(c, float(importance_map.get(c, 0.0))) for c in cols],
                   key=lambda x: x[1], reverse=True)
    ranked = [c for c, w in ranked if w >= floor]

    if topk > 0:
        ranked = ranked[:topk]

    return ranked

# ---------- 6) 总控：训练前的特征准备 ----------
def prepare_features_for_training(df: pd.DataFrame,
                                  programs: Dict[str, Any],
                                  program_id: str,
                                  fit_mask: pd.Series,
                                  importance_map: Optional[Dict[str, float]] = None) -> Tuple[List[str], Any]:
    """
    统一特征准备流程：
    1. 构建特征（零拷贝）
    2. 缺失处理
    3. 标准化
    4. 低方差+相关性剪枝
    5. 重要性剪枝（可选）

    Returns:
        (feature_cols, scaler)
    """
    meta = programs["global"]["meta"]
    std_meta = meta.get("standardize", {})
    nan_meta = meta.get("nan_policy", {})
    prune_meta = meta.get("pruning", {})
    prog = programs["programs"][program_id]
    post_rules = prog.get("post_rules", {})

    # 1) 构建（零拷贝）
    cols = apply_feature_program(df, programs, program_id)

    # 2) 覆盖/缺失处理
    cols = apply_nan_policy(df, cols, nan_meta)

    # 3) 标准化（仅特征列）
    cols, scaler = standardize_block(df, cols, std_meta, fit_mask)

    # 4) 低方差 + 相关性剪枝
    var_floor = float(post_rules.get("var_floor", prune_meta.get("var_floor", 1e-8)))
    corr_thr = float(post_rules.get("corr_threshold", prune_meta.get("corr_threshold", 0.95)))
    cols = prune_by_variance_and_corr(df, cols, var_floor, corr_thr)

    # 5) 重要性剪枝（若提供）
    if importance_map is not None:
        floor = float(post_rules.get("importance_floor", 0.0))
        topk = int(post_rules.get("keep_top_k", prune_meta.get("max_features", 128)))
        cols = prune_by_importance(importance_map, cols, floor, topk)
    else:
        topk = int(post_rules.get("keep_top_k", prune_meta.get("max_features", 128)))
        cols = cols[:topk]

    return cols, scaler
