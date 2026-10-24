# featuretools_adapter.py
# SPDX-License-Identifier: Apache-2.0
#
# Featuretools DFS适配器：IS拟合定义/OOS复用定义，防泄漏
from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Set
import pandas as pd
import numpy as np

try:
    import featuretools as ft
    HAS_FEATURETOOLS = True
except ImportError:
    HAS_FEATURETOOLS = False
    ft = None


class FeaturetoolsAdapter:
    """
    Featuretools DFS适配器

    核心功能：
    1. IS上fit：定义EntitySet + DFS生成特征定义
    2. OOS上transform：严格复用IS的特征定义（防泄漏）
    3. cutoff_horizon_sec：防止使用未来数据
    4. training_window：限制回看窗口
    5. topk_by_variance：控制特征维度
    6. 持久化特征定义到JSON

    防泄漏机制：
    - cutoff_horizon_sec: 每个样本的cutoff_time = ts - horizon
    - OOS严格复用IS定义，不重新计算DFS
    - 时间窗口严格限制
    """

    def __init__(self, config: Dict[str, Any]):
        if not HAS_FEATURETOOLS:
            raise ImportError("featuretools not installed. pip install featuretools")

        self.config = config
        self.feature_defs_ = None
        self.entity_name_ = config.get("entity_name", "bars_1m")
        self.index_ = config.get("index", "row_id")
        self.time_index_ = config.get("time_index", "ts")
        self.symbol_col_ = config.get("symbol_col", "symbol")
        self.cutoff_horizon_sec_ = int(config.get("cutoff_horizon_sec", 60))
        self.training_window_ = config.get("training_window", "30D")
        self.base_cols_: List[str] = []

    def fit_on_is(self, df_is: pd.DataFrame, base_cols: List[str]) -> "FeaturetoolsAdapter":
        """
        在IS数据上拟合：生成特征定义

        Args:
            df_is: IS数据框（必须包含index, time_index, symbol_col及基础特征）
            base_cols: 基础特征列名（用于变量类型推断）

        Returns:
            self
        """
        self.base_cols_ = list(base_cols)

        # 准备数据
        df = df_is.copy()

        # 确保有row_id
        if self.index_ not in df.columns:
            df[self.index_] = range(len(df))

        # 确保时间列是datetime
        if not pd.api.types.is_datetime64_any_dtype(df[self.time_index_]):
            df[self.time_index_] = pd.to_datetime(df[self.time_index_])

        # 1) 创建EntitySet
        es = ft.EntitySet(id="training_data")

        # 推断变量类型（避免Featuretools误判）
        variable_types = self._infer_variable_types(df)

        es = es.add_dataframe(
            dataframe_name=self.entity_name_,
            dataframe=df,
            index=self.index_,
            time_index=self.time_index_,
            logical_types=variable_types
        )

        # 2) DFS生成特征定义
        max_depth = int(self.config.get("max_depth", 2))
        agg_primitives = self.config.get("agg_primitives", ["mean", "std", "max", "min"])
        trans_primitives = self.config.get("trans_primitives", ["day", "hour", "weekday"])

        # 生成特征矩阵（用于得到特征定义）
        # 使用cutoff_time防泄漏
        cutoff_times = self._make_cutoff_times(df)

        feature_matrix, feature_defs = ft.dfs(
            entityset=es,
            target_dataframe_name=self.entity_name_,
            cutoff_time=cutoff_times,
            training_window=self.training_window_,
            max_depth=max_depth,
            agg_primitives=agg_primitives,
            trans_primitives=trans_primitives,
            verbose=False
        )

        # 3) 过滤特征
        feature_defs = self._filter_features(feature_defs, feature_matrix)

        # 4) 降维：按方差排序保留topk
        topk = int(self.config.get("topk_by_variance", 1024))
        if len(feature_defs) > topk:
            # 计算方差
            var = feature_matrix[[f.get_name() for f in feature_defs]].var()
            var_sorted = var.sort_values(ascending=False)
            keep_names = set(var_sorted.index[:topk])
            feature_defs = [f for f in feature_defs if f.get_name() in keep_names]

        self.feature_defs_ = feature_defs

        # 5) 持久化定义
        self._save_feature_defs()

        return self

    def transform_any(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        使用IS定义的特征在任意数据上transform

        Args:
            df: 数据框（IS或OOS）

        Returns:
            特征矩阵
        """
        if self.feature_defs_ is None:
            raise RuntimeError("Must call fit_on_is() first")

        # 准备数据
        df = df.copy()

        if self.index_ not in df.columns:
            df[self.index_] = range(len(df))

        if not pd.api.types.is_datetime64_any_dtype(df[self.time_index_]):
            df[self.time_index_] = pd.to_datetime(df[self.time_index_])

        # 创建EntitySet
        es = ft.EntitySet(id="transform_data")

        variable_types = self._infer_variable_types(df)

        es = es.add_dataframe(
            dataframe_name=self.entity_name_,
            dataframe=df,
            index=self.index_,
            time_index=self.time_index_,
            logical_types=variable_types
        )

        # 使用cutoff_time
        cutoff_times = self._make_cutoff_times(df)

        # 计算特征（复用定义）
        feature_matrix = ft.calculate_feature_matrix(
            features=self.feature_defs_,
            entityset=es,
            cutoff_time=cutoff_times,
            training_window=self.training_window_,
            verbose=False
        )

        # 删除索引列（只保留新特征）
        if self.index_ in feature_matrix.columns:
            feature_matrix = feature_matrix.drop(columns=[self.index_])

        # 填充NaN（DFS可能产生缺失值）
        feature_matrix = feature_matrix.fillna(0.0)

        return feature_matrix.astype(np.float32)

    def _infer_variable_types(self, df: pd.DataFrame) -> Dict[str, str]:
        """推断变量类型（Featuretools的logical_types）"""
        from featuretools.variable_types import (
            Numeric, Categorical, Datetime, Index
        )

        types = {}

        for col in df.columns:
            if col == self.index_:
                types[col] = Index
            elif col == self.time_index_:
                types[col] = Datetime
            elif col == self.symbol_col_:
                types[col] = Categorical
            elif pd.api.types.is_numeric_dtype(df[col]):
                types[col] = Numeric
            elif pd.api.types.is_datetime64_any_dtype(df[col]):
                types[col] = Datetime
            else:
                types[col] = Categorical

        return types

    def _make_cutoff_times(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        生成cutoff_time DataFrame（防泄漏）

        每个样本的cutoff_time = ts - cutoff_horizon_sec
        """
        cutoff = pd.DataFrame({
            self.index_: df[self.index_],
            "time": df[self.time_index_] - pd.Timedelta(seconds=self.cutoff_horizon_sec_)
        })
        return cutoff

    def _filter_features(self, feature_defs: List, feature_matrix: pd.DataFrame) -> List:
        """过滤不需要的特征"""
        drop_contains = self.config.get("drop_contains", ["lead", "future"])
        drop_exact = set(self.config.get("drop_exact", []))

        keep = []
        for f in feature_defs:
            name = f.get_name()

            # 跳过精确匹配
            if name in drop_exact:
                continue

            # 跳过包含黑名单词的特征
            if any(word in name.lower() for word in drop_contains):
                continue

            # 跳过基础列（避免重复）
            if name in self.base_cols_:
                continue

            # 跳过常量特征
            if name in feature_matrix.columns:
                if feature_matrix[name].nunique() <= 1:
                    continue

            keep.append(f)

        return keep

    def _save_feature_defs(self):
        """持久化特征定义到JSON"""
        save_path = self.config.get("save_feature_defs")
        if not save_path:
            return

        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 将特征定义序列化为字符串列表
        defs_str = [f.get_name() for f in self.feature_defs_]

        meta = {
            "n_features": len(self.feature_defs_),
            "feature_names": defs_str,
            "config": self.config,
            "base_cols": self.base_cols_
        }

        # 追加模式（保留历史）
        if path.exists():
            try:
                history = json.loads(path.read_text())
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("failed_to_load_feature_history", error=str(e))
                history = []
        else:
            history = []

        history.append(meta)

        path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
