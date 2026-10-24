# xgb_feature_maker.py
# SPDX-License-Identifier: Apache-2.0
#
# XGB Feature Factory：把 XGB 当"特征工厂"（叶子嵌入 + Hash降维）
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional

try:
    from xgboost import XGBRegressor, XGBClassifier
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    XGBRegressor = None
    XGBClassifier = None


class XGBFeatureMaker:
    """
    XGBoost叶子嵌入特征生成器

    核心功能：
    1. 训练XGB模型（回归/二分类/多分类）
    2. 提取叶子索引（每个样本的决策路径）
    3. 转换为特征：
       - "onehot"模式：稀疏one-hot编码（高维，不推荐）
       - "hash"模式：Hash trick降维（推荐，hash_dim=512~2048）

    使用场景：
    - Pipeline模式"xgb_then_dfs": XGB叶子特征→合并原始特征→DFS
    - Pipeline模式"xgb_only": 仅使用XGB叶子特征
    """

    def __init__(self, cfg: Dict[str, Any], task: str):
        """
        Args:
            cfg: feature_tools.json中的xgb_feature_maker配置
            task: "regression" | "binary" | "multiclass"
        """
        if not HAS_XGBOOST:
            raise ImportError("XGBoost not installed. pip install xgboost")

        self.cfg = cfg
        self.task = task
        self.model = None

        # 叶子嵌入模式
        leaf_cfg = cfg.get("leaf_embedding", {})
        self.mode = (leaf_cfg.get("mode", "hash")).lower()
        self.hash_dim = int(leaf_cfg.get("hash_dim", 512))

    def fit(self, X: pd.DataFrame, y: pd.Series, sample_weight=None):
        """
        训练XGB模型

        Args:
            X: 特征矩阵
            y: 标签
            sample_weight: 样本权重（可选）

        Returns:
            self
        """
        params = self.cfg.get("model", {})

        if self.task == "binary":
            self.model = XGBClassifier(**params)
        elif self.task == "multiclass":
            n_cls = int(y.max()) + 1
            self.model = XGBClassifier(num_class=n_cls, **params)
        else:
            self.model = XGBRegressor(**params)

        self.model.fit(X, y, sample_weight=sample_weight, verbose=False)
        return self

    def _leaf_index(self, X: pd.DataFrame) -> np.ndarray:
        """
        提取叶子索引

        Args:
            X: 特征矩阵

        Returns:
            shape=(n_samples, n_trees) 的叶子索引数组
        """
        return self.model.apply(X)

    def _embed(self, leaves: np.ndarray) -> pd.DataFrame:
        """
        将叶子索引转换为特征

        Args:
            leaves: shape=(n_samples, n_trees) 的叶子索引

        Returns:
            特征DataFrame
        """
        # 确保leaves是2维数组
        if leaves.ndim == 1:
            leaves = leaves.reshape(-1, 1)

        if self.mode == "onehot":
            # 稀疏one-hot编码（可能很大，不建议）
            codes = leaves.astype("int64")
            cols = []
            for j in range(codes.shape[1]):
                col = pd.get_dummies(
                    pd.Series(codes[:, j]),
                    prefix=f"leaf{j}",
                    sparse=True
                )
                cols.append(col)
            return pd.concat(cols, axis=1)

        # Hash trick（推荐）
        h = np.zeros((leaves.shape[0], self.hash_dim), dtype=np.float32)
        for j in range(leaves.shape[1]):
            # 简单 hash：leaf_id -> bucket
            # 使用大质数避免碰撞
            bucket = (leaves[:, j].astype("int64") * (j + 1315423911)) % self.hash_dim
            for i, b in enumerate(bucket):
                h[i, b] += 1.0

        return pd.DataFrame(h, index=None).add_prefix("xgbh_")

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        """
        生成XGB叶子特征

        Args:
            X: 特征矩阵

        Returns:
            叶子嵌入特征DataFrame
        """
        if self.model is None:
            raise RuntimeError("Must call fit() before transform()")

        leaves = self._leaf_index(X)
        return self._embed(leaves)

    def fit_transform(self, X: pd.DataFrame, y: pd.Series, sample_weight=None) -> pd.DataFrame:
        """
        训练并生成特征（便捷方法）

        Args:
            X: 特征矩阵
            y: 标签
            sample_weight: 样本权重（可选）

        Returns:
            叶子嵌入特征DataFrame
        """
        self.fit(X, y, sample_weight=sample_weight)
        return self.transform(X)
