#!/usr/bin/env python3
"""
Model-based ML Inference Service

加载训练好的 XGBoost/LSTM 模型进行实时推理
替代 RealMLInferenceService (基于 OOF 查询)
"""
from datetime import datetime, date
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
import json
import numbers

# Lazy imports
try:
    from libs.logger import get_logger

    logger = get_logger(__name__)
except ImportError:  # pragma: no cover - fallback
    import logging

    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

try:
    import xgboost as xgb
    import numpy as np
except ImportError:  # pragma: no cover - environment without xgboost
    raise ImportError("xgboost not installed. Run: pip install xgboost")


class ModelMLInferenceService:
    """Real ML inference using trained model files."""

    def __init__(self, model_dir: str = "models/xgb_h30_tsla_optimized"):
        """
        Initialize model inference service.

        Args:
            model_dir: Path to model directory containing:
                - model.json: XGBoost model file
                - features.json: Feature metadata
                - metadata.json: Training metadata
        """
        self.model_dir = Path(model_dir)

        if not self.model_dir.exists():
            raise FileNotFoundError(f"Model directory not found: {model_dir}")

        self.model: Optional[xgb.Booster] = None
        self.feature_cols: List[str] = []
        self.metadata: Dict[str, Any] = {}
        self.scaler = None  # Optional RobustScaler or other preprocessor
        self.is_ready: bool = False
        # Optional per-column status (static + runtime) loaded from feature_status configs
        self._feature_status: Dict[str, str] = {}

        # Load model
        model_path = self.model_dir / "model.json"
        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")

        self.model = xgb.Booster()
        self.model.load_model(str(model_path))

        # Load feature metadata
        features_path = self.model_dir / "features.json"
        if features_path.exists():
            with open(features_path) as f:
                feature_info = json.load(f)
                self.feature_cols = feature_info['feature_cols']
        else:
            # Fallback to default feature list
            self.feature_cols = self._get_default_features()

        # Load training metadata
        metadata_path = self.model_dir / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as f:
                self.metadata = json.load(f)
                logger.info("model_loaded",
                          model_type=self.metadata.get('model_type'),
                          label=self.metadata.get('label'),
                          symbol=self.metadata.get('symbol'),
                          accuracy=self.metadata.get('cv_results', {}).get('avg_accuracy'))
        else:
            self.metadata = {}

        # Optional: load scaler for downstream outlier detection / strict inference
        try:
            scaler_path = self.model_dir / "scaler.pkl"
            if scaler_path.exists():
                try:
                    import joblib  # type: ignore

                    self.scaler = joblib.load(scaler_path)
                    logger.info("model_scaler_loaded", path=str(scaler_path))
                except Exception as exc:  # pragma: no cover - best-effort
                    logger.warning("model_scaler_load_failed", error=str(exc))
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("model_scaler_setup_failed", error=str(exc))

        self.is_ready = True
        logger.info(
            "model_ml_inference_initialized",
            model_dir=str(model_dir),
            n_features=len(self.feature_cols),
        )

        # Load optional feature_status metadata so that gateway can treat
        # low-coverage/disabled columns as optional rather than hard-failing
        # the entire feature vector.
        try:
            self._load_feature_status()
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("feature_status_load_failed", error=str(exc))

    def _load_feature_status(self) -> None:
        """
        Load static + runtime feature status to determine which columns are
        optional at inference time.

        - config/feature_status.json: operator-maintained long-term flags
        - runs/ai_trader/<today>/feature_status.runtime.json: auto-generated
          by tools/auto_update_feature_status_runtime.py based on coverage.
        """
        status: Dict[str, str] = {}

        def _load(path: Path) -> Dict[str, Any]:
            if not path.exists():
                return {}
            try:
                return json.loads(path.read_text("utf-8"))
            except Exception:
                return {}

        static_status = _load(Path("config/feature_status.json"))
        today = date.today().isoformat()
        runtime_status = _load(Path("runs") / "ai_trader" / today / "feature_status.runtime.json")

        for src in (static_status, runtime_status):
            cols = src.get("columns") or {}
            if isinstance(cols, dict):
                for name, st in cols.items():
                    if isinstance(name, str) and isinstance(st, str):
                        status[name] = st

        self._feature_status = status or {}
        if self._feature_status:
            logger.info(
                "feature_status_loaded",
                n_columns=len(self._feature_status),
            )

    def _is_optional_feature(self, col: str) -> bool:
        """
        Determine whether a feature column should be treated as optional at
        inference time (i.e., missing/NaN values do not hard-fail the vector).

        Convention:
          - Any status starting with 'disabled' marks the column as optional.
          - Other statuses (enabled/empty/missing) are treated as required.
        """
        if not self._feature_status:
            return False
        st = self._feature_status.get(col)
        if not isinstance(st, str):
            return False
        return st.strip().lower().startswith("disabled")

    def _get_default_features(self) -> List[str]:
        """Get default feature list (matches training data)."""
        return [
            'close', 'volume', 'vwap',
            'atr_14', 'atr_5',
            'ema_10', 'ema_20', 'ema_50',
            'sma_5', 'sma_20', 'sma_60',
            'rsi_14',
            'stoch_k_14', 'stoch_d_3',
            'roc_5', 'roc_10',
            'std_20', 'std_60',
            'rv_20', 'parkinson_20', 'garman_klass_20',
            'bb_mid_20', 'bb_up_20', 'bb_dn_20', 'bb_bw_20', 'bb_pct_20',
            'di_plus_14', 'di_minus_14', 'dx_14', 'adx_14',
            'vol_ema_20', 'vol_spike', 'vol_avg_20', 'rvol_roll_20',
            'z_price_20', 'z_price_60',
            'slope_20', 'r2_20',
        ]

    def _prepare_features(self, features: Dict[str, Any]) -> xgb.DMatrix:
        """
        Prepare DMatrix from raw features dict.

        Args:
            features: Dict with feature values

        Returns:
            xgb.DMatrix with feature names
        """
        # Extract feature values in correct order
        X = []
        for col in self.feature_cols:
            value = features.get(col, 0.0)  # Default to 0 if missing
            X.append(float(value))

        arr = np.array(X).reshape(1, -1)

        # Create DMatrix with feature names (required for model validation)
        return xgb.DMatrix(arr, feature_names=self.feature_cols)

    # === Strict vectorization / raw prediction for Gateway/outlier layers ===

    def _vectorize_strict(self, features: Dict[str, Any]) -> Tuple[Optional[np.ndarray], Optional[str]]:
        """
        Strictly vectorize features: no None/NaN/Inf allowed.

        Returns:
            (vector, error_reason)
        """
        if not self.feature_cols:
            return None, "no_feature_metadata"

        vec: List[float] = []
        missing: List[str] = []

        for col in self.feature_cols:
            val = features.get(col)
            invalid = (
                val is None
                or not isinstance(val, numbers.Number)
                or np.isnan(val)
                or np.isinf(val)
            )
            if invalid:
                # Columns marked as disabled/low-coverage in feature_status are
                # treated as optional: we fill them with 0.0 but do NOT count
                # them as missing for the purpose of invalid_features gating.
                if self._is_optional_feature(col):
                    vec.append(0.0)
                    continue
                missing.append(col)
                vec.append(0.0)
            else:
                vec.append(float(val))

        if missing:
            # Do not use the vector if any required feature is invalid; gateway can
            # decide how to handle this error_reason. Optional (disabled) features
            # do not contribute to this count.
            reason = f"invalid_features: {len(missing)} ({','.join(missing[:3])}...)"
            return None, reason

        return np.asarray(vec, dtype=float), None

    def predict_raw(self, features: Dict[str, Any]) -> Dict[str, Any]:
        """
        Low-level prediction API used by ML Gateway.

        - Performs strict feature validation (no NaN/Inf/None).
        - Applies optional scaler if present.
        - Returns raw probability and both raw/scaled vectors.
        """
        if not self.is_ready or self.model is None:
            return {"status": "error", "error_reason": "service_not_ready", "raw_pred": 0.5}

        try:
            raw_vec, err = self._vectorize_strict(features)
            if err or raw_vec is None:
                return {"status": "error", "error_reason": err or "vectorization_failed", "raw_pred": 0.5}

            if self.scaler is not None:
                vec_2d = raw_vec.reshape(1, -1)
                try:
                    scaled_vec = self.scaler.transform(vec_2d)
                except Exception as exc:
                    logger.warning("model_scaler_transform_failed", error=str(exc))
                    scaled_vec = vec_2d
            else:
                scaled_vec = raw_vec.reshape(1, -1)

            dmatrix = xgb.DMatrix(scaled_vec, feature_names=self.feature_cols)
            prob = float(self.model.predict(dmatrix)[0])

            return {
                "status": "ok",
                "raw_pred": prob,
                "raw_vector": raw_vec,
                "scaled_vector": scaled_vec.flatten(),
                "features_valid": True,
            }
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("predict_raw_failed", error=str(exc), exc_info=True)
            return {"status": "error", "error_reason": "crash", "raw_pred": 0.5}

    def predict(self, features: Dict[str, Any]) -> Dict[str, float]:
        """
        Generate ML predictions from model.

        Args:
            features: Feature dict containing symbol, ts, and all required features

        Returns:
            Dict with keys:
                - ml_signal: +1.0 (buy) / -1.0 (sell)
                - ml_conf: 0.0-1.0 confidence
                - xgb_pred_raw: raw XGBoost probability
                - (future: lstm_pred_raw, fusion_pred_raw)
        """
        symbol = features.get("symbol", "UNKNOWN")
        ts = features.get("ts", datetime.now())

        try:
            # Prepare features (returns DMatrix with feature names)
            dmatrix = self._prepare_features(features)

            # Predict
            xgb_pred = float(self.model.predict(dmatrix)[0])

            # For now, only XGBoost (LSTM can be added later)
            # fusion_pred = xgb_pred  # TODO: Add LSTM ensemble

            # Convert to signal and confidence
            ml_signal = 1.0 if xgb_pred > 0.5 else -1.0
            ml_conf = abs(xgb_pred - 0.5) * 2.0  # [0, 1]

            logger.debug("ml_prediction_generated",
                        symbol=symbol,
                        ts=str(ts),
                        xgb_pred=round(xgb_pred, 4),
                        ml_signal=ml_signal,
                        ml_conf=round(ml_conf, 4))

            return {
                # Primary ML predictions
                "ml_signal": ml_signal,
                "ml_conf": ml_conf,

                # Individual model predictions
                "xgb_signal": ml_signal,
                "xgb_conf": ml_conf,
                "xgb_pred_raw": xgb_pred,

                # Placeholder for future models
                "lstm_signal": 0.0,
                "lstm_conf": 0.0,
                "lstm_pred_raw": 0.5,
                "fusion_signal": ml_signal,
                "fusion_conf": ml_conf,
                "fusion_pred_raw": xgb_pred,
            }

        except Exception as e:
            logger.error("prediction_failed", symbol=symbol, error=str(e), exc_info=True)
            return self._neutral_prediction()

    def _neutral_prediction(self) -> Dict[str, float]:
        """Return neutral prediction (no trade signal)."""
        return {
            "ml_signal": 0.0,
            "ml_conf": 0.0,
            "xgb_signal": 0.0,
            "xgb_conf": 0.0,
            "lstm_signal": 0.0,
            "lstm_conf": 0.0,
            "fusion_signal": 0.0,
            "fusion_conf": 0.0,
            "xgb_pred_raw": 0.5,
            "lstm_pred_raw": 0.5,
            "fusion_pred_raw": 0.5,
        }

    def predict_batch(self, features_list: list) -> list:
        """
        Generate predictions for multiple bars.

        Args:
            features_list: List of feature dicts

        Returns:
            List of prediction dicts
        """
        return [self.predict(f) for f in features_list]


# Singleton instance
_model_service: Optional[ModelMLInferenceService] = None


def get_model_service(model_dir: str = "models/xgb_h30_tsla_optimized") -> ModelMLInferenceService:
    """Get or create singleton model service instance."""
    global _model_service
    if _model_service is None:
        _model_service = ModelMLInferenceService(model_dir=model_dir)
    return _model_service


if __name__ == "__main__":
    # Test the model service
    import sys

    print("Testing Model ML Inference Service")
    print("=" * 60)

    try:
        service = ModelMLInferenceService()

        # Test with sample features
        test_features = {
            "symbol": "TSLA",
            "ts": datetime.now(),
            "close": 250.0,
            "volume": 1000000,
            "vwap": 249.5,
            "atr_14": 5.0,
            "atr_5": 4.5,
            "ema_10": 248.0,
            "ema_20": 247.0,
            "ema_50": 245.0,
            "sma_5": 249.0,
            "sma_20": 246.0,
            "sma_60": 244.0,
            "rsi_14": 55.0,
            "stoch_k_14": 60.0,
            "stoch_d_3": 58.0,
            "roc_5": 0.01,
            "roc_10": 0.02,
            "std_20": 3.0,
            "std_60": 4.0,
            "rv_20": 0.02,
            "parkinson_20": 0.025,
            "garman_klass_20": 0.023,
            "bb_mid_20": 250.0,
            "bb_up_20": 255.0,
            "bb_dn_20": 245.0,
            "bb_bw_20": 10.0,
            "bb_pct_20": 0.5,
            "di_plus_14": 25.0,
            "di_minus_14": 20.0,
            "dx_14": 20.0,
            "adx_14": 25.0,
            "vol_ema_20": 900000,
            "vol_spike": 1.1,
            "vol_avg_20": 950000,
            "rvol_roll_20": 1.05,
            "z_price_20": 0.5,
            "z_price_60": 0.8,
            "slope_20": 0.01,
            "r2_20": 0.8,
        }

        print("\nTest prediction:")
        prediction = service.predict(test_features)

        print(f"  Symbol: TSLA")
        print(f"  ML Signal: {prediction['ml_signal']:+.1f}")
        print(f"  ML Confidence: {prediction['ml_conf']:.3f}")
        print(f"  XGB pred: {prediction['xgb_pred_raw']:.4f}")

        print("\n✅ Model ML Inference Service is working!")

    except Exception as e:
        print(f"\n❌ Service test failed: {e}")
        import traceback
        traceback.print_exc()
