# loss_factory.py
# SPDX-License-Identifier: Apache-2.0
#
# 损失/目标 DSL：回归/分类/排序 + XGBoost自定义焦点损失
from __future__ import annotations
from typing import Dict, Any, Tuple, Optional, Callable
import numpy as np

try:
    import xgboost as xgb
except Exception:
    xgb = None

def get_objective_and_metric(loss_spec: Dict[str, Any]) -> Tuple[Optional[Callable], Optional[str], Dict[str, Any]]:
    """
    根据损失配置返回训练目标和参数

    Args:
        loss_spec: 损失配置字典
        {
          "family": "regression|classification|ranking",
          "name": "mse|mae|huber|bce|focal|pairwise|...",
          "extras": {额外参数}
        }

    Returns:
        (custom_obj, xgb_obj_name, extra_params)
        - custom_obj: XGBoost 自定义梯度/海森目标（如 focal）
        - xgb_obj_name: 直接用 XGBoost 内置（如 reg:squarederror, binary:logistic, rank:pairwise）
        - extra_params: 训练器额外参数（如 scale_pos_weight）

    支持的损失函数：

    **regression**:
    - mse, square, l2 → reg:squarederror
    - mae, l1 → reg:absoluteerror
    - huber, pseudo_huber → reg:pseudohubererror
    - quantile, pinball → 需要额外处理（LightGBM或sklearn）

    **classification**:
    - bce, logloss, ce → binary:logistic
    - focal, focal_loss → 自定义Focal Loss（处理类别不平衡）

    **ranking**:
    - pairwise, rank:pairwise → rank:pairwise
    """
    family = loss_spec.get("family", "regression")
    name   = loss_spec.get("name", "mse").lower()
    extras = loss_spec.get("extras", {}) or {}
    extra_params: Dict[str, Any] = {}

    if (xgb is None) and family in ("regression","classification","ranking"):
        # 没有 xgboost 时，调用方应回退 sklearn/lightgbm
        return None, None, {}

    if family == "regression":
        if name in ("mse","square","l2"):
            return None, "reg:squarederror", extra_params

        if name in ("mae","l1"):
            return None, "reg:absoluteerror", extra_params

        if name in ("huber","pseudo_huber"):
            # XGB 提供 reg:pseudohubererror
            return None, "reg:pseudohubererror", extra_params

        if name in ("quantile","pinball"):
            alpha = float(extras.get("alpha", 0.5))
            # LightGBM 原生；XGB 需自定义，这里暂留给 LightGBM/SklearnQuantile
            extra_params["quantile_alpha"] = alpha
            return None, "reg:squarederror", extra_params  # 训练器侧做回退映射

        return None, "reg:squarederror", extra_params

    if family == "classification":
        if name in ("bce","logloss","ce"):
            spw = extras.get("scale_pos_weight", None)
            if spw is not None: extra_params["scale_pos_weight"] = float(spw)
            return None, "binary:logistic", extra_params

        if name in ("focal","focal_loss"):
            gamma = float(extras.get("gamma", 2.0))
            alpha = float(extras.get("alpha", 0.25))

            def focal_obj(preds: np.ndarray, dtrain: "xgb.DMatrix"):
                """
                Focal Loss for XGBoost
                FL = - alpha*y*(1-p)^gamma * log(p) - (1-alpha)*(1-y)*p^gamma * log(1-p)
                """
                y = dtrain.get_label()
                p = 1.0 / (1.0 + np.exp(-preds))  # sigmoid

                # Focal loss gradient/hessian
                # dFL/dz = (p - y) * weight(p,y)  标准实现
                w_pos = alpha * (1 - p) ** gamma
                w_neg = (1 - alpha) * p ** gamma
                g = (p - y) * np.where(y > 0.5, w_pos, w_neg)

                # 近似 Hessian：p*(1-p)*w + gamma 调整项（简化）
                h = p * (1 - p) * np.where(y > 0.5, w_pos, w_neg)

                return g, h

            return focal_obj, None, extra_params

        return None, "binary:logistic", extra_params

    if family == "ranking":
        # 直接用 XGB 排序目标；需要 group 信息由上层提供
        if name in ("pairwise","rank:pairwise"):
            return None, "rank:pairwise", extra_params
        return None, "rank:pairwise", extra_params

    # 组合/投资组合类目标通常在"训练后评估"阶段处理；此处回落到回归
    return None, "reg:squarederror", extra_params
