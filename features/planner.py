# features/planner.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Iterable

from features.registry import FeatureRegistry
from features.standard_features import PYTHON_DERIVED_FEATURES


@dataclass
class FeatureMaterializationPlan:
    """
    描述这次训练/实验真正要用到的特征物化计划。
    """
    base_columns: List[str]        # 需要在 df 中存在的原始列（来自 ClickHouse）
    python_features: List[str]     # 需要通过 add_standard_features 计算的标准特征
    final_feature_list: List[str]  # 最终喂给模型的特征列表（顺序固定）
    dropped_features: List[str]    # 请求中被丢弃的特征名（记录日志用）


def plan_from_recipe(
    recipe: Dict[str, Any],
    registry: FeatureRegistry,
    *,
    available_columns: Iterable[str],
) -> FeatureMaterializationPlan:
    """
    根据 LLM 给出的 feature_recipe + Registry + 实际可用列，
    决定最终用哪些特征。

    - 优先信任 Registry（知道哪些是 python 特征 / 原始列）
    - 其次 fallback 到：
        * 在 PYTHON_DERIVED_FEATURES 里 → python 特征
        * 在 available_columns 里 → 原始列
        * 其他 → 丢弃并记录
    """
    available_set = set(available_columns)
    requested = list(recipe.get("base_features") or [])

    base_cols: List[str] = []
    python_feats: List[str] = []
    dropped: List[str] = []

    def _safe_add(unique_list: List[str], name: str) -> None:
        if name not in unique_list:
            unique_list.append(name)

    for name in requested:
        fdef = registry.get(name)
        if fdef is not None:
            # 1) Registry 里有定义
            if fdef.kind == "python":
                # python 型特征，必须在标准 python 特征列表里存在
                if name in PYTHON_DERIVED_FEATURES:
                    # 确保它依赖的 base_cols 在 df 中存在
                    missing_base = [c for c in fdef.base_cols if c not in available_set]
                    if missing_base:
                        dropped.append(name)
                        # 缺基础列，暂时放弃该特征
                    else:
                        _safe_add(python_feats, name)
                        for c in fdef.base_cols:
                            _safe_add(base_cols, c)
                else:
                    # Registry 标记为 python，但我们目前还没有对应实现
                    dropped.append(name)
            else:
                # native 型特征，要求 df 中有同名列
                if name in available_set:
                    _safe_add(base_cols, name)
                else:
                    dropped.append(name)
        else:
            # 2) Registry 没有定义，fallback 策略
            if name in PYTHON_DERIVED_FEATURES:
                _safe_add(python_feats, name)
            elif name in available_set:
                _safe_add(base_cols, name)
            else:
                dropped.append(name)

    # SQL 里已经算出来的 derived 特征（expr AS alias）
    derived_aliases = [
        d.get("alias")
        for d in recipe.get("derived", []) or []
        if d.get("alias")
    ]

    # 最终特征列表 = 原始列 + python 特征 + derived 列
    final_features: List[str] = []
    for n in base_cols:
        _safe_add(final_features, n)
    for n in python_feats:
        _safe_add(final_features, n)
    for n in derived_aliases:
        _safe_add(final_features, n)

    return FeatureMaterializationPlan(
        base_columns=base_cols,
        python_features=python_feats,
        final_feature_list=final_features,
        dropped_features=dropped,
    )
