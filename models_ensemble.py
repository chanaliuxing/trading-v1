# models_ensemble.py
# 加权平均 Ensemble: 支持任意组合 (XGB + Lasso + Ridge + LightGBM + ...)
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional
import numpy as np

# Import existing models
from models import XGBModel, LSTMModel, TransformerBaseline, ETSformer, iTransformer, DecisionScheme
from models_linear import (
    LassoModel,
    RidgeModel,
    ElasticNetModel,
    LogisticModel,
    LinearSVMModel,
    KNNModel,
    LightGBMModel,
    RuleFitModel,
)


@dataclass
class EnsembleMetadata:
    name: str
    task: str
    sub_models: List[Dict[str, Any]]


# Simple model registry: string → class
MODEL_REGISTRY = {
    "xgb": XGBModel,
    "xgboost": XGBModel,
    "lstm": LSTMModel,
    "transformer": TransformerBaseline,
    "transformer_baseline": TransformerBaseline,
    "transformerbaseline": TransformerBaseline,
    "etsformer": ETSformer,
    "itransformer": iTransformer,
    "i-transformer": iTransformer,
    "lasso": LassoModel,
    "ridge": RidgeModel,
    "elasticnet": ElasticNetModel,
    "elastic_net": ElasticNetModel,
    "logreg": LogisticModel,
    "logistic": LogisticModel,
    "svm_linear": LinearSVMModel,
    "knn": KNNModel,
    "lgbm": LightGBMModel,
    "lightgbm": LightGBMModel,
    "rulefit": RuleFitModel,
    "rule_fit": RuleFitModel,
}


class EnsembleModel:
    """
    简单的加权平均模型池:
    - sub_models: [(weight, model_instance), ...]
    - 统一 .fit / .predict / .to_signal 接口
    - 和 XGBModel/LSTMModel 等 duck-typing 兼容

    配置格式:
    {
      "model_type": "ensemble",
      "ensemble": {
        "models": [
          {"type": "xgb", "weight": 0.5, "task": "reg", "hparams": {...}},
          {"type": "lasso", "weight": 0.3, "task": "reg", "hparams": {...}},
          {"type": "ridge", "weight": 0.2, "task": "reg"}
        ]
      }
    }
    """

    def __init__(
        self,
        task: str,
        hparams: Dict[str, Any],
        decision_scheme: Optional[DecisionScheme],
        ensemble_spec: Dict[str, Any],
    ):
        self.task = task or "reg"
        self.hparams = dict(hparams or {})
        self.decision_scheme = decision_scheme
        self.ensemble_spec = ensemble_spec or {}
        self.sub_models: List[Tuple[float, Any]] = []

        # 解析 ensemble_spec
        models_cfg = self.ensemble_spec.get("models") or []
        if not models_cfg:
            raise ValueError("EnsembleModel 需要 ensemble_spec['models'] 非空")

        for m_cfg in models_cfg:
            m_type = str(m_cfg.get("type", "")).lower()
            weight = float(m_cfg.get("weight", 1.0))
            extra_hparams = dict(m_cfg.get("hparams") or {})

            if m_type not in MODEL_REGISTRY:
                raise ValueError(f"EnsembleModel: unknown sub model_type={m_type}")

            ModelCls = MODEL_REGISTRY[m_type]

            # 子模型的 hparams = 顶层 hparams 覆盖 + 专属 hparams
            sub_hparams = dict(self.hparams)
            sub_hparams.update(extra_hparams)

            # 对于分类子模型，task 必须是 "clf"
            sub_task = m_cfg.get("task", self.task)
            model = ModelCls(task=sub_task, hparams=sub_hparams, decision_scheme=decision_scheme)

            self.sub_models.append((weight, model))

        # 归一化权重
        w_sum = sum(w for w, _ in self.sub_models)
        if w_sum <= 0:
            # 全 0 就平均
            n = len(self.sub_models)
            self.sub_models = [(1.0 / n, m) for _, m in self.sub_models]
        else:
            self.sub_models = [(w / w_sum, m) for w, m in self.sub_models]

        print(f"[EnsembleModel] Initialized with {len(self.sub_models)} sub-models:")
        for i, (w, m) in enumerate(self.sub_models, 1):
            print(f"  {i}. {m.__class__.__name__} (weight={w:.3f})")

    # === 统一接口 ===

    def fit(self, X, y):
        print(f"[EnsembleModel] Training {len(self.sub_models)} sub-models...")
        for i, (w, m) in enumerate(self.sub_models, 1):
            print(f"  Training {i}/{len(self.sub_models)}: {m.__class__.__name__} (weight={w:.3f})")
            m.fit(X, y)
        print(f"[EnsembleModel] All sub-models trained successfully")
        return self

    def predict(self, X):
        """
        子模型先各自 predict:
        - 回归: 直接数值
        - 分类: 输出正类概率 [0, 1]
        然后按权重加权平均
        """
        preds = []
        weights = []
        for w, m in self.sub_models:
            p = m.predict(X)
            p = np.asarray(p, dtype=float)
            preds.append(p)
            weights.append(w)

        preds_arr = np.vstack(preds)  # shape: [n_models, n_samples]
        w_arr = np.asarray(weights, dtype=float)[:, None]
        weighted_pred = (preds_arr * w_arr).sum(axis=0)

        return weighted_pred

    def to_signal(self, preds):
        preds = np.asarray(preds, dtype=float)
        if self.task == "clf":
            # 0.5 概率 → { -1, +1 }
            return np.where(preds >= 0.5, 1.0, -1.0)
        else:
            # 回归: 符号 + 截断幅度
            return np.clip(np.sign(preds) * np.minimum(np.abs(preds), 1.0), -1.0, 1.0)

    def metadata(self) -> EnsembleMetadata:
        subs = []
        for w, m in self.sub_models:
            meta = getattr(m, "metadata", None)
            if callable(meta):
                m_meta = meta()
                if hasattr(m_meta, "__dict__"):
                    m_meta = m_meta.__dict__
            else:
                m_meta = {"type": m.__class__.__name__}
            subs.append({"weight": w, "meta": m_meta})

        return EnsembleMetadata(
            name="EnsembleModel",
            task=self.task,
            sub_models=subs,
        )
