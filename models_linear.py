# models_linear.py
# 轻量版"机构级"线性 / KNN / LightGBM 模型适配器
# 支持: Lasso, Ridge, ElasticNet, Logistic, LinearSVM, KNN, LightGBM, RuleFit
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional
import numpy as np

from sklearn.linear_model import Lasso, Ridge, ElasticNet, LogisticRegression
from sklearn.svm import LinearSVC
from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor

try:
    import lightgbm as lgb  # type: ignore
except Exception:
    lgb = None

try:
    from skrules import RuleFit  # type: ignore
except Exception:
    RuleFit = None


@dataclass
class ModelMetadata:
    name: str
    model_type: str
    task: str
    hparams: Dict[str, Any]


class _BaseSklearnAdapter:
    """
    满足 wf_hpo 里用到的最小接口:
    - .task: "reg" or "clf"
    - .fit(X, y)
    - .predict(X) -> np.ndarray
    - .to_signal(preds) -> np.ndarray
    - .metadata() -> dataclass / dict
    """

    def __init__(self, task: str, hparams: Dict[str, Any], decision_scheme: Any = None):
        self.task = task or "reg"
        self.hparams = dict(hparams or {})
        self.decision_scheme = decision_scheme
        self._model = self._build_model(self.hparams)

    # 子类必须实现
    def _build_model(self, hparams: Dict[str, Any]):
        raise NotImplementedError

    def fit(self, X, y):
        self._model.fit(X, y)
        return self

    def predict(self, X):
        # 对于回归：返回连续值
        # 对于分类：返回 [0,1] 概率（和 XGBModel 一致，方便 wf_hpo 的 conf 计算）
        if self.task == "clf":
            # 默认尝试 predict_proba
            if hasattr(self._model, "predict_proba"):
                proba = self._model.predict_proba(X)
                if isinstance(proba, list):
                    proba = np.asarray(proba)
                # 二分类，取正类概率
                if proba.ndim == 2 and proba.shape[1] >= 2:
                    return proba[:, 1]
            # 没有 predict_proba 就退化到 decision_function -> sigmoid
            if hasattr(self._model, "decision_function"):
                df = self._model.decision_function(X)
                df = np.asarray(df, dtype=float)
                return 1.0 / (1.0 + np.exp(-df))
            # 最后 fallback: label -> {0,1}
            labels = self._model.predict(X)
            labels = np.asarray(labels, dtype=float)
            return np.clip(labels, 0.0, 1.0)
        else:
            preds = self._model.predict(X)
            return np.asarray(preds, dtype=float)

    def to_signal(self, preds: np.ndarray) -> np.ndarray:
        preds = np.asarray(preds, dtype=float)
        if self.task == "clf":
            # 0.5 为分界，映射成 {-1, +1}
            return np.where(preds >= 0.5, 1.0, -1.0)
        else:
            # 回归：直接用符号，幅度裁剪到 [-1, 1]
            return np.clip(np.sign(preds) * np.minimum(np.abs(preds), 1.0), -1.0, 1.0)

    def metadata(self) -> ModelMetadata:
        return ModelMetadata(
            name=self.__class__.__name__,
            model_type=self.__class__.__name__,
            task=self.task,
            hparams=self.hparams,
        )


# ---------- 线性回归类 ----------

class LassoModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if self.task != "reg":
            raise ValueError("LassoModel 只推荐用于 task='reg'")
        alpha = float(hparams.get("alpha", 0.001))
        max_iter = int(hparams.get("max_iter", 1000))
        tol = float(hparams.get("tol", 1e-4))
        return Lasso(alpha=alpha, max_iter=max_iter, tol=tol)


class RidgeModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if self.task != "reg":
            raise ValueError("RidgeModel 只推荐用于 task='reg'")
        alpha = float(hparams.get("alpha", 1.0))
        max_iter = int(hparams.get("max_iter", 1000))
        tol = float(hparams.get("tol", 1e-4))
        return Ridge(alpha=alpha, max_iter=max_iter, tol=tol)


class ElasticNetModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if self.task != "reg":
            raise ValueError("ElasticNetModel 只推荐用于 task='reg'")
        alpha = float(hparams.get("alpha", 0.01))
        l1_ratio = float(hparams.get("l1_ratio", 0.5))
        max_iter = int(hparams.get("max_iter", 2000))
        tol = float(hparams.get("tol", 1e-4))
        return ElasticNet(alpha=alpha, l1_ratio=l1_ratio, max_iter=max_iter, tol=tol)


# ---------- 线性分类类 ----------

class LogisticModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if self.task != "clf":
            raise ValueError("LogisticModel 只推荐用于 task='clf'")
        C = float(hparams.get("C", 1.0))
        max_iter = int(hparams.get("max_iter", 200))
        penalty = hparams.get("penalty", "l2")
        solver = hparams.get("solver", "lbfgs")
        return LogisticRegression(
            C=C,
            max_iter=max_iter,
            penalty=penalty,
            solver=solver,
            n_jobs=-1,
        )


class LinearSVMModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if self.task != "clf":
            raise ValueError("LinearSVMModel 只推荐用于 task='clf'")
        C = float(hparams.get("C", 1.0))
        max_iter = int(hparams.get("max_iter", 2000))
        return LinearSVC(C=C, max_iter=max_iter)


# ---------- KNN ----------

class KNNModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        n_neighbors = int(hparams.get("n_neighbors", 15))
        weights = hparams.get("weights", "distance")
        if self.task == "clf":
            return KNeighborsClassifier(n_neighbors=n_neighbors, weights=weights, n_jobs=-1)
        else:
            return KNeighborsRegressor(n_neighbors=n_neighbors, weights=weights, n_jobs=-1)


# ---------- LightGBM ----------

class LightGBMModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if lgb is None:
            raise ImportError("LightGBMModel 需要安装 lightgbm: pip install lightgbm")

        params = dict(hparams)
        # 基础参数
        params.setdefault("learning_rate", 0.05)
        params.setdefault("num_leaves", 31)
        params.setdefault("max_depth", -1)
        params.setdefault("min_child_samples", 20)
        params.setdefault("subsample", 0.8)
        params.setdefault("colsample_bytree", 0.8)
        params.setdefault("n_estimators", 200)
        params.setdefault("n_jobs", -1)
        params.setdefault("verbose", -1)

        if self.task == "clf":
            params.setdefault("objective", "binary")
            params.setdefault("metric", "binary_logloss")
            return lgb.LGBMClassifier(**params)
        else:
            params.setdefault("objective", "regression")
            params.setdefault("metric", "rmse")
            return lgb.LGBMRegressor(**params)


# ---------- RuleFit ----------

class RuleFitModel(_BaseSklearnAdapter):
    def _build_model(self, hparams: Dict[str, Any]):
        if RuleFit is None:
            raise ImportError("RuleFitModel 需要安装 sklearn-rulefit: pip install sklearn-rulefit")

        tree_size = float(hparams.get("tree_size", 4.0))
        sample_fract = float(hparams.get("sample_fract", 0.75))
        max_rules = int(hparams.get("max_rules", 2000))
        memory_par = float(hparams.get("memory_par", 0.01))

        if self.task == "clf":
            rf = RuleFit(
                tree_size=tree_size,
                sample_fract=sample_fract,
                max_rules=max_rules,
                memory_par=memory_par,
                rfmode="classification",
            )
        else:
            rf = RuleFit(
                tree_size=tree_size,
                sample_fract=sample_fract,
                max_rules=max_rules,
                memory_par=memory_par,
                rfmode="regression",
            )
        return rf


# ---------- 模型池注册表 ----------

MODEL_POOL = {
    "lasso": LassoModel,
    "ridge": RidgeModel,
    "elasticnet": ElasticNetModel,
    "logistic": LogisticModel,
    "linear_svm": LinearSVMModel,
    "linearsvm": LinearSVMModel,  # Alias
    "knn": KNNModel,
    "lightgbm": LightGBMModel,
    "lgbm": LightGBMModel,  # Alias
    "rulefit": RuleFitModel,
}


def get_linear_model_class(model_type: str):
    """
    根据 model_type 字符串返回对应的模型类。

    Args:
        model_type: 模型类型名称（不区分大小写）

    Returns:
        对应的模型类

    Raises:
        KeyError: 如果model_type不在MODEL_POOL中
    """
    mt = (model_type or "").lower()
    if mt not in MODEL_POOL:
        available = list(MODEL_POOL.keys())
        raise KeyError(
            f"Unknown linear model_type={model_type!r}. "
            f"Available models: {available}"
        )
    return MODEL_POOL[mt]
