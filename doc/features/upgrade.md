这是最终确定的**生产级落地版本**。

我们达成的一致核心原则是：**“机制与策略分离，参数完全数据驱动”**。

1.  **Inference Service**：只做“计算器”，负责算分，不负责判断。
2.  **Gateway**：做“指挥官”，负责读取训练元数据（Metadata），动态设定风控阈值，决定是“开火”还是“熔断”。
3.  **Outlier Detector**：做“数学工具”，只提供统计结论。

以下是**4 个核心文件**的代码及**落地执行清单**。

-----

### 📂 1. 数学工具层：`services/ml/outlier_detector.py`

**职责**：纯粹的统计计算，无业务逻辑。检查单样本是否偏离训练集分布。

```python
import numpy as np
from typing import Dict, List, Any
from sklearn.preprocessing import RobustScaler
from libs.logger import get_logger

class OutlierDetector:
    """
    Real-time Outlier Detector (Stateless Tool)
    基于训练集 RobustScaler 的统计量 (Center/Scale) 计算当前样本的异常度。
    """
    def __init__(self, scaler: RobustScaler, feature_cols: List[str]):
        self.scaler = scaler
        self.feature_cols = feature_cols
        # 提取统计量 (防止除零)
        self.center = scaler.center_ if hasattr(scaler, 'center_') else np.zeros(len(feature_cols))
        scale = scaler.scale_ if hasattr(scaler, 'scale_') else np.ones(len(feature_cols))
        self.scale = np.where(scale == 0, 1.0, scale)

    def check(self, vector: np.ndarray, z_threshold: float = 10.0, ratio_limit: float = 0.3) -> Dict[str, Any]:
        """
        Args:
            vector: 已清洗的特征向量 (Raw or Scaled depending on usage, usually Raw for Z-score)
            z_threshold: 单特征多少倍 IQR 算异常 (动态传入)
            ratio_limit: 多少比例特征异常算整条废弃 (动态传入)
        """
        # 计算 Robust Z-Score: |x - median| / IQR
        z_scores = np.abs((vector - self.center) / self.scale)
        
        # 统计异常
        outliers = z_scores > z_threshold
        n_outliers = np.sum(outliers)
        ratio = n_outliers / len(vector)
        
        is_abnormal = ratio > ratio_limit
        
        result = {
            "is_outlier": is_abnormal,
            "outlier_score": float(ratio),
            "max_z": float(np.max(z_scores)) if len(z_scores) > 0 else 0.0,
            "details": []
        }
        
        if is_abnormal:
            # 记录偏差最大的前3个特征，方便日志排查
            top_idx = np.argsort(z_scores)[-3:][::-1]
            result["details"] = [
                f"{self.feature_cols[i]}(z={z_scores[i]:.1f})" 
                for i in top_idx if z_scores[i] > z_threshold
            ]
            
        return result
```

-----

### 📂 2. 基础服务层：`services/ml/model_inference_service.py`

**职责**：加载模型/Scaler，执行纯推理。**Fail-Fast**（有脏数据直接报错，不填 0）。

```python
#!/usr/bin/env python3
import joblib
import json
import numpy as np
import numbers
import xgboost as xgb
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from libs.logger import get_logger

class ModelMLInferenceService:
    """
    Lean ML Inference Service (The Calculator)
    职责：资源加载、Scaler转换、原始概率计算。不包含风控逻辑。
    """
    
    def __init__(self, model_dir: str = "models/xgb_h30_tsla_optimized"):
        self.log = get_logger("ml.inference.service")
        self.model_dir = Path(model_dir)
        self.is_ready = False
        self.scaler = None
        self.metadata = {}
        
        try:
            self._load_resources()
            self.is_ready = True
            self.log.info("ml_service_ready", model=self.model_dir.name, n_features=len(self.feature_cols))
        except Exception as e:
            self.log.error("ml_service_init_failed", error=str(e))
            # 允许初始化失败，predict 时再拦截

    def _load_resources(self):
        # 1. Model
        model_path = self.model_dir / "model.json"
        if not model_path.exists():
            raise FileNotFoundError(f"Missing model: {model_path}")
        self.model = xgb.Booster()
        self.model.load_model(str(model_path))
        
        # 2. Features
        feat_path = self.model_dir / "features.json"
        if not feat_path.exists():
            raise FileNotFoundError(f"Missing feature def: {feat_path}")
        with open(feat_path) as f:
            self.feature_cols = json.load(f)['feature_cols']
            
        # 3. Scaler
        scaler_path = self.model_dir / "scaler.pkl"
        if scaler_path.exists():
            self.scaler = joblib.load(scaler_path)
        else:
            self.log.warning("no_scaler_found_using_raw_features")

        # 4. Metadata (Gateway 将读取此数据)
        meta_path = self.model_dir / "metadata.json"
        if meta_path.exists():
            try:
                self.metadata = json.load(open(meta_path))
            except Exception:
                self.metadata = {}

    def predict_raw(self, features: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行纯推理。
        Returns: {status, raw_pred, raw_vector, scaled_vector, error_reason}
        """
        if not self.is_ready:
            return {"status": "error", "error_reason": "service_not_ready", "raw_pred": 0.5}

        try:
            # 1. Vectorize (Strict Check)
            raw_vec, error = self._vectorize(features)
            if error:
                return {"status": "error", "error_reason": error, "raw_pred": 0.5}

            # 2. Scale
            if self.scaler:
                vec_2d = raw_vec.reshape(1, -1)
                scaled_vec = self.scaler.transform(vec_2d)
            else:
                scaled_vec = raw_vec.reshape(1, -1)

            # 3. Predict
            dmatrix = xgb.DMatrix(scaled_vec, feature_names=self.feature_cols)
            prob = float(self.model.predict(dmatrix)[0])

            return {
                "status": "ok",
                "raw_pred": prob,
                "raw_vector": raw_vec,            # 供 OutlierDetector 用
                "scaled_vector": scaled_vec.flatten(),
                "features_valid": True
            }

        except Exception as e:
            self.log.error("inference_exception", error=str(e), exc_info=True)
            return {"status": "error", "error_reason": "crash", "raw_pred": 0.5}

    def _vectorize(self, features: Dict) -> Tuple[Optional[np.ndarray], Optional[str]]:
        """严苛的数据检查：不允许 NaN/Inf/None"""
        vec = []
        missing = []
        
        for col in self.feature_cols:
            val = features.get(col)
            if (val is None or 
                not isinstance(val, numbers.Number) or 
                np.isnan(val) or 
                np.isinf(val)):
                missing.append(col)
                vec.append(0.0) # 占位，但会报错
            else:
                vec.append(float(val))
        
        if missing:
            return None, f"invalid_features: {len(missing)} ({','.join(missing[:3])}...)"
            
        return np.array(vec), None
```

-----

### 📂 3. 决策层：`services/ml/ai_brain_ml_gateway.py`

**职责**：从 Metadata 读取参数，判断 Regime/Drift，最终决定信号。这是你的“预测前安全网”。

```python
#!/usr/bin/env python3
from typing import Dict, Any
from libs.logger import get_logger
from services.ml.model_inference_service import ModelMLInferenceService
from services.ml.outlier_detector import OutlierDetector

class AiBrainMLGateway:
    """
    AI Brain ML Gateway (The Decision Maker)
    原则：所有风控参数优先从 metadata 读取，绝不硬编码。
    """
    
    def __init__(self, inference_service: ModelMLInferenceService):
        self.service = inference_service
        self.log = get_logger("ml.gateway")
        
        # 初始化工具
        self.outlier_detector = None
        if self.service.is_ready and self.service.scaler:
            self.outlier_detector = OutlierDetector(self.service.scaler, self.service.feature_cols)

        # 加载动态配置
        self._reload_dynamic_config()

    def _reload_dynamic_config(self):
        """从模型 Metadata 加载训练得到的参数"""
        meta = self.service.metadata
        opt = meta.get("optimized_thresholds", {})
        
        # 1. 信号阈值
        self.signal_high = opt.get("signal_threshold_high", 0.55)
        self.signal_low = opt.get("signal_threshold_low", 0.45)
        
        # 2. 市场状态阈值 (Regime)
        self.baseline_atr = meta.get("baseline_atr_14", 0.0)
        self.regime_max_atr_mult = opt.get("regime_max_atr_mult", 3.0)
        
        # 3. 离群检测配置 (Outlier)
        drift_cfg = opt.get("drift_config", {})
        self.z_threshold = drift_cfg.get("z_threshold", 10.0)
        self.outlier_ratio = drift_cfg.get("outlier_ratio_threshold", 0.3)
        
        self.log.info("gateway_config_updated", 
                     signal_cfg=(self.signal_low, self.signal_high),
                     regime_cfg=f"{self.regime_max_atr_mult}x_ATR",
                     outlier_cfg=drift_cfg)

    def get_decision(self, symbol: str, features: Dict[str, Any]) -> Dict[str, Any]:
        """
        核心决策流：Inference -> Outlier Check -> Regime Check -> Signal
        """
        # 1. 基础推理
        result = self.service.predict_raw(features)
        if result["status"] != "ok":
            return self._block(symbol, "data_integrity", result.get("error_reason"))

        raw_pred = result["raw_pred"]
        raw_vec = result["raw_vector"] # 用原始值算 z-score 更直观

        # 2. 离群值检测 (Safety Net)
        drift_score = 0.0
        if self.outlier_detector:
            outlier_res = self.outlier_detector.check(
                raw_vec, 
                z_threshold=self.z_threshold, 
                ratio_limit=self.outlier_ratio
            )
            drift_score = outlier_res["outlier_score"]
            
            if outlier_res["is_outlier"]:
                return self._block(
                    symbol, "outlier_drift", 
                    f"features_deviated: {', '.join(outlier_res['details'])}",
                    drift_score=drift_score
                )

        # 3. 市场状态过滤 (Regime Filter)
        current_atr = features.get("atr_14")
        if self.baseline_atr > 0 and current_atr:
            if current_atr > self.baseline_atr * self.regime_max_atr_mult:
                return self._block(
                    symbol, "regime_filter", 
                    f"volatility_spike: {current_atr:.2f} > {self.baseline_atr * self.regime_max_atr_mult:.2f}",
                    drift_score=drift_score
                )

        # 4. 信号生成
        ml_signal = 0.0
        if raw_pred > self.signal_high:
            ml_signal = 1.0
        elif raw_pred < self.signal_low:
            ml_signal = -1.0
        
        ml_conf = abs(raw_pred - 0.5) * 2.0 if ml_signal != 0 else 0.0

        return {
            "decision": "trade" if ml_signal != 0 else "hold",
            "ml_signal": ml_signal,
            "ml_conf": ml_conf,
            "raw_pred": raw_pred,
            "drift_score": drift_score,
            "status": "ok"
        }

    def _block(self, symbol: str, code: str, details: str, **kwargs) -> Dict:
        self.log.warning("gateway_blocked", symbol=symbol, code=code, details=details)
        return {
            "decision": "blocked",
            "block_reason": f"[{code}] {details}",
            "ml_signal": 0.0,
            "ml_conf": 0.0,
            "raw_pred": 0.5,
            "status": "blocked",
            **kwargs
        }
```

-----

### 📂 4. 训练源头：`scripts/train_institutional_grade.py` (片段)

**职责**：计算并写入 `metadata.json`，这是整个“数据驱动”逻辑的源头。

```python
# ... (模型训练结束后) ...

# 1. 计算动态阈值
train_atr_mean = df['atr_14'].mean()
val_preds = model.predict_proba(X_val)[:, 1]

# 自动确定置信度阈值 (例如: 前 20% 高确信度区间)
high_thr = float(np.percentile(val_preds, 80))
low_thr = float(np.percentile(val_preds, 20))
# 保底逻辑，不能低于默认值
high_thr = max(0.55, high_thr)
low_thr = min(0.45, low_thr)

# 2. 构建 Metadata
metadata = {
    "model_type": "xgboost",
    "training_period": [str(train_start), str(train_end)],
    "feature_names": all_features,
    
    # ✅ 核心：数据驱动的基准线
    "baseline_atr_14": float(train_atr_mean),
    
    # ✅ 核心：优化的风控参数
    "optimized_thresholds": {
        "signal_threshold_high": high_thr,
        "signal_threshold_low": low_thr,
        "regime_max_atr_mult": 4.0, # 可以设为 3.0 或 4.0
        "drift_config": {
            "z_threshold": 10.0,    # 允许单特征偏离 10 倍 IQR
            "outlier_ratio_threshold": 0.3
        }
    }
}

# 3. 保存所有 Artifacts
import joblib
import json
model.save_model(str(model_dir / "model.json"))
joblib.dump(scaler, model_dir / "scaler.pkl")
with open(model_dir / "features.json", "w") as f:
    json.dump({"feature_cols": all_features}, f, indent=2)
with open(model_dir / "metadata.json", "w") as f:
    json.dump(metadata, f, indent=2)

print("✅ 模型包构建完成: model + scaler + features + metadata")
```

-----

### ✅ 落地执行 Todo List (Copy-Paste Action)

1.  **创建文件**:
      * 新建 `services/ml/outlier_detector.py`
      * 更新 `services/ml/model_inference_service.py`
      * 新建 `services/ml/ai_brain_ml_gateway.py`
2.  **更新训练脚本**:
      * 修改 `scripts/train_institutional_grade.py`，加入上述 metadata 保存逻辑。
3.  **重新训练模型**:
      * 运行一次训练脚本，生成包含 `scaler.pkl` 和 `metadata.json` 的新模型包。
4.  **集成到控制器**:
      * 在 `LLMTraderController` 或策略代码中：
        ```python
        # 初始化
        srv = ModelMLInferenceService("models/你的新模型路径")
        gateway = AiBrainMLGateway(srv)

        # 使用
        decision = gateway.get_decision(symbol, features)
        if decision['decision'] == 'trade':
            # 下单
        elif decision['decision'] == 'blocked':
            # 记录日志: decision['block_reason']
        ```

这一套改完，你的 AI 系统就真正具备了\*\*自我感知（Drift）**和**自我保护（Regime Block）\*\*的能力，且所有参数都有据可依。