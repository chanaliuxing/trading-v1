# feature_filter.py
# SPDX-License-Identifier: Apache-2.0
#
# LLM/JSON 可控的轻量级特征筛选器
from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

# sklearn is optional; degrade gracefully
try:
    from sklearn.feature_selection import mutual_info_classif, mutual_info_regression
except Exception:  # pragma: no cover
    mutual_info_classif = None
    mutual_info_regression = None


class FeatureFilter:
    """
    LLM/JSON 可控的轻量级特征筛选器：
      • 低方差剔除
      • 目标相关度门槛（回归: Spearman | 分类: MI/点双列相关）
      • 特征两两高相关去冗余（贪心保留与目标更相关者）
      • 可选 Information Value（分类）
      • 最大保留列数上限
    —— 在 IS 上 fit，OOS 上仅 transform（防泄漏）。
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.selected_: List[str] = []
        self.reasons_: Dict[str, Dict[str, Any]] = {}
        self.task_ = (self.cfg.get("task") or "regression").lower()
        self.meta_path_ = None

    # -------- public API --------
    def fit(self, X: pd.DataFrame, y: pd.Series) -> "FeatureFilter":
        X = X.copy()
        y = y.copy()
        self.reasons_.clear()

        filters = self.cfg.get("filters", {})
        # 0) 漏洞守卫：可按名字规则先过滤
        X = self._leakage_guard(X, filters.get("leakage_guard", {}))

        cols = list(X.columns)
        keep = set(cols)

        # 1) 低方差
        keep = keep & self._variance_gate(X, filters.get("variance", {}))

        # 2) 与目标的相关性/互信息/IV
        keep = keep & self._target_relevance_gate(X[list(keep)], y, filters)

        # 3) 两两高相关去冗余（在剩余列上）
        keep = self._pair_corr_cull(X[list(keep)], y, filters.get("pair_corr", {}), filters)

        # 4) 上限（按与目标相关性/MI/方差排序）
        keep = self._cap_max_features(X[list(keep)], y, filters)

        self.selected_ = list(keep)
        self._maybe_save_meta()
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self.selected_:
            return X
        present = [c for c in self.selected_ if c in X.columns]
        return X[present].copy()

    def fit_transform(self, X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
        return self.fit(X, y).transform(X)

    # -------- gates --------
    def _leakage_guard(self, X: pd.DataFrame, p: Dict[str, Any]) -> pd.DataFrame:
        if not p:
            return X
        blacklist = set(map(str.lower, p.get("name_blacklist", ["label", "future", "lead"])) )
        drop_cols = [c for c in X.columns if any(b in c.lower() for b in blacklist)]
        if drop_cols:
            X = X.drop(columns=drop_cols)
            for c in drop_cols:
                self.reasons_[c] = {"drop": "leakage_guard"}
        return X

    def _variance_gate(self, X: pd.DataFrame, p: Dict[str, Any]) -> set:
        vmin = float(p.get("min", 1e-12))
        var = X.var(axis=0, numeric_only=True).fillna(0.0)
        keep = set(var[var > vmin].index)
        for c in X.columns:
            if c not in keep:
                self.reasons_[c] = {"drop": "low_variance", "var": float(var.get(c, 0.0))}
        return keep

    def _target_relevance_gate(self, X: pd.DataFrame, y: pd.Series, filters: Dict[str, Any]) -> set:
        task = self.task_
        keep = set(X.columns)
        # 回归：Spearman/pearson；分类：MI 或 点双列相关
        targ = filters.get("target_corr", {})
        use_tcorr = bool(targ.get("enabled", True))
        tc_metric = str(targ.get("metric", "spearman")).lower()
        tc_min = float(targ.get("min_abs", 0.0))
        sample_n = int(targ.get("sample_n", 50000))

        # MI（可选）
        mi_cfg = filters.get("mi", {})
        use_mi = bool(mi_cfg.get("enabled", False))
        mi_min = float(mi_cfg.get("min", 0.0))
        mi_sample = int(mi_cfg.get("sample_n", 50000))

        idx = X.index
        if len(idx) > max(sample_n, mi_sample):
            rs = 1337
            idx = X.sample(n=max(sample_n, mi_sample), random_state=rs).index
        Xs = X.loc[idx]
        ys = y.loc[idx]

        rel = pd.Series(0.0, index=X.columns)
        # 先算 tcorr
        if use_tcorr:
            if tc_metric == "spearman":
                corr = Xs.rank().corrwith(ys.rank(), method="pearson")  # spearman 等价于秩相关
            elif tc_metric == "pearson":
                corr = Xs.corrwith(ys, method="pearson")
            else:
                corr = Xs.rank().corrwith(ys.rank(), method="pearson")
            corr = corr.abs().fillna(0.0)
            rel = np.maximum(rel.values, corr.reindex(X.columns).fillna(0.0).values)
            drop = corr[corr < tc_min].index
            for c in drop:
                keep.discard(c)
                self.reasons_[c] = {"drop": "low_target_corr", "abs_corr": float(corr.get(c, 0.0))}

        # 再算 MI（分类/回归均可用）
        if use_mi and (mutual_info_classif is not None or mutual_info_regression is not None):
            try:
                if task.startswith("class"):
                    mi = mutual_info_classif(Xs.values, ys.values, discrete_features=False)
                else:
                    mi = mutual_info_regression(Xs.values, ys.values, discrete_features=False)
                mi = pd.Series(mi, index=X.columns)
                rel = np.maximum(rel.values, mi.reindex(X.columns).fillna(0.0).values)
                drop = mi[mi < mi_min].index
                for c in drop:
                    keep.discard(c)
                    self.reasons_[c] = {"drop": "low_mi", "mi": float(mi.get(c, 0.0))}
            except Exception:
                pass

        # 存 relevance
        for i, c in enumerate(X.columns):
            self.reasons_.setdefault(c, {})["relevance"] = float(rel[i])
        return keep

    def _pair_corr_cull(self, X: pd.DataFrame, y: pd.Series, p: Dict[str, Any], filters: Dict[str, Any]) -> set:
        if not bool(p.get("enabled", True)):
            return set(X.columns)
        max_abs = float(p.get("max_abs", 0.98))
        metric = str(p.get("metric", "spearman")).lower()
        sample_n = int(p.get("sample_n", 50000))

        idx = X.index
        if len(idx) > sample_n:
            idx = X.sample(n=sample_n, random_state=1337).index
        Xs = X.loc[idx]

        # 相关矩阵
        if metric == "pearson":
            C = Xs.corr(method="pearson").abs()
        else:
            C = Xs.rank().corr(method="pearson").abs()  # spearman
        np.fill_diagonal(C.values, 0.0)

        # 贪心去冗：按"与目标相关性/MI/方差"的代理排序，保留强者
        score = pd.Series({c: self.reasons_.get(c, {}).get("relevance", 0.0) for c in X.columns})
        # fallback：方差
        var = X.var().fillna(0.0)
        score = score + (var / (var.max() + 1e-12)) * 1e-6  # 打破平局

        keep = set(X.columns)
        visited = set()
        for c in score.sort_values(ascending=False).index:
            if c in visited:
                continue
            visited.add(c)
            # 找与 c 高相关的其它列
            high = C.index[(C[c] >= max_abs)].tolist()
            for h in high:
                if h == c or h not in keep:
                    continue
                # 比较得分保留强者
                if score[c] >= score[h]:
                    keep.discard(h)
                    self.reasons_[h] = {"drop": "high_pair_corr", "with": c, "rho": float(C.loc[h, c])}
                else:
                    keep.discard(c)
                    self.reasons_[c] = {"drop": "high_pair_corr", "with": h, "rho": float(C.loc[h, c])}
                    break
        return keep

    def _cap_max_features(self, X: pd.DataFrame, y: pd.Series, filters: Dict[str, Any]) -> set:
        k = int(filters.get("max_features", 4096))
        if k <= 0 or X.shape[1] <= k:
            return set(X.columns)
        # 复用 relevance 排序截断
        rel = pd.Series({c: self.reasons_.get(c, {}).get("relevance", 0.0) for c in X.columns})
        take = set(rel.sort_values(ascending=False).index[:k])
        drop = set(X.columns) - take
        for c in drop:
            self.reasons_[c] = {"drop": "cap_max_features"}
        return take

    # -------- meta --------
    def _maybe_save_meta(self):
        filters = self.cfg.get("filters", {})
        if not bool(filters.get("save_meta", False)):
            return
        path = Path(filters.get("meta_path", "runs/feature_registry/feature_filter_meta.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "task": self.task_,
            "selected": self.selected_,
            "reasons": self.reasons_
        }
        if path.exists():
            try:
                obj = json.loads(path.read_text())
            except Exception:
                obj = []
        else:
            obj = []
        obj.append(meta)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
