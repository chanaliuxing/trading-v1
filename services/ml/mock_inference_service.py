#!/usr/bin/env python3
"""
Mock ML Inference Service - Generates synthetic predictions for Phase 0 testing.

This is a TEMPORARY service to verify the soft threshold system works correctly.
It generates predictions with distribution matching the training metrics:
- XGB: mean=0.35, std=0.15
- LSTM: mean=0.01, std=0.01
- Fusion: mean=0.22, std=0.12

TODO: Replace with real model inference once deployment pipeline is built.
"""

import numpy as np
from typing import Dict, Any, Optional
from libs.logger import get_logger

logger = get_logger(__name__)


class MockMLInferenceService:
    """Mock ML inference that generates synthetic predictions for testing."""

    def __init__(self, seed: Optional[int] = None):
        """
        Initialize mock inference service.

        Args:
            seed: Random seed for reproducibility (None = use random)
        """
        # Production safety check: block instantiation in production and paper trading
        import os
        TRADING_MODE = os.getenv("TRADING_MODE", "dev").lower()
        if TRADING_MODE in {"production", "prod", "live", "paper"}:
            raise RuntimeError(
                "MockMLInferenceService is disabled in live/paper trading. "
                "This is a test-only service that generates synthetic predictions. "
                "Use ModelMLInferenceService or InferenceManager for live/paper trading."
            )

        self.rng = np.random.RandomState(seed)

        # Distribution parameters matching training metrics from
        # logs/control_event_consumer.log @ 20:35:44
        self.xgb_mean = 0.353
        self.xgb_std = 0.15
        self.lstm_mean = 0.009
        self.lstm_std = 0.01
        self.fusion_mean = 0.215
        self.fusion_std = 0.12

        logger.info(
            "mock_ml_inference_initialized",
            xgb_mean=self.xgb_mean,
            lstm_mean=self.lstm_mean,
            fusion_mean=self.fusion_mean,
        )

    def predict(self, features: Dict[str, Any]) -> Dict[str, float]:
        """
        Generate mock predictions for a single bar.

        Args:
            features: Feature dict (not used in mock, but kept for API compatibility)

        Returns:
            Dict with keys: ml_signal, ml_conf, xgb_signal, xgb_conf,
                           lstm_signal, lstm_conf, fusion_signal, fusion_conf
        """
        # Generate XGB prediction (higher quality)
        xgb_pred = self.rng.normal(self.xgb_mean, self.xgb_std)
        xgb_pred = float(np.clip(xgb_pred, 0.0, 1.0))
        xgb_signal = 1.0 if xgb_pred > 0.5 else -1.0

        # Generate LSTM prediction (very weak)
        lstm_pred = self.rng.normal(self.lstm_mean, self.lstm_std)
        lstm_pred = float(np.clip(lstm_pred, 0.0, 1.0))
        lstm_signal = 1.0 if lstm_pred > 0.5 else -1.0

        # Generate Fusion prediction (weighted average)
        fusion_pred = self.rng.normal(self.fusion_mean, self.fusion_std)
        fusion_pred = float(np.clip(fusion_pred, 0.0, 1.0))
        fusion_signal = 1.0 if fusion_pred > 0.5 else -1.0

        # Primary signal is fusion (matches strategy config)
        ml_signal = fusion_signal
        ml_conf = abs(fusion_pred - 0.5) * 2.0  # Convert [0,1] to confidence [0,1]

        return {
            # Primary ML predictions (used by strategy)
            "ml_signal": ml_signal,
            "ml_conf": ml_conf,
            # Individual model predictions (for debugging/logging)
            "xgb_signal": xgb_signal,
            "xgb_conf": abs(xgb_pred - 0.5) * 2.0,
            "lstm_signal": lstm_signal,
            "lstm_conf": abs(lstm_pred - 0.5) * 2.0,
            "fusion_signal": fusion_signal,
            "fusion_conf": ml_conf,
            # Raw predictions (before confidence conversion)
            "xgb_pred_raw": xgb_pred,
            "lstm_pred_raw": lstm_pred,
            "fusion_pred_raw": fusion_pred,
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


def inject_mock_predictions(features: Dict[str, Any], service: MockMLInferenceService) -> Dict[str, Any]:
    """
    Inject mock ML predictions into features dict.

    This function is called by the feature provider to add ML predictions
    before passing features to strategies.

    Args:
        features: Original features dict (OHLCV, ATR, etc.)
        service: MockMLInferenceService instance

    Returns:
        Features dict with ML predictions added
    """
    predictions = service.predict(features)

    # Merge predictions into features
    features_with_ml = features.copy()
    features_with_ml.update(predictions)

    return features_with_ml


# Singleton instance for easy import
_mock_service: Optional[MockMLInferenceService] = None


def get_mock_service(seed: Optional[int] = None) -> MockMLInferenceService:
    """Get or create singleton mock service instance."""
    global _mock_service
    if _mock_service is None:
        _mock_service = MockMLInferenceService(seed=seed)
    return _mock_service


if __name__ == "__main__":
    # Test the mock service
    import json

    service = MockMLInferenceService(seed=42)

    # Generate 100 predictions to verify distribution
    predictions = [service.predict({}) for _ in range(100)]

    # Calculate statistics
    xgb_confs = [p["xgb_conf"] for p in predictions]
    lstm_confs = [p["lstm_conf"] for p in predictions]
    fusion_confs = [p["fusion_conf"] for p in predictions]

    print("Mock ML Inference Service - Statistics (n=100)")
    print("=" * 60)
    print(f"XGB   - mean: {np.mean(xgb_confs):.3f}, std: {np.std(xgb_confs):.3f}, min: {np.min(xgb_confs):.3f}, max: {np.max(xgb_confs):.3f}")
    print(f"LSTM  - mean: {np.mean(lstm_confs):.3f}, std: {np.std(lstm_confs):.3f}, min: {np.min(lstm_confs):.3f}, max: {np.max(lstm_confs):.3f}")
    print(f"Fusion- mean: {np.mean(fusion_confs):.3f}, std: {np.std(fusion_confs):.3f}, min: {np.min(fusion_confs):.3f}, max: {np.max(fusion_confs):.3f}")
    print()

    # Show distribution across confidence zones
    hard_reject = sum(1 for c in fusion_confs if c < 0.05)
    soft_reduce = sum(1 for c in fusion_confs if 0.05 <= c < 0.30)
    normal = sum(1 for c in fusion_confs if c >= 0.30)

    print("Confidence Zone Distribution:")
    print(f"  Hard Reject (<5%):     {hard_reject:3d} ({hard_reject}%)")
    print(f"  Soft Reduce (5-30%):   {soft_reduce:3d} ({soft_reduce}%)")
    print(f"  Normal (>=30%):        {normal:3d} ({normal}%)")
    print()

    # Sample predictions
    print("Sample Predictions:")
    for i, p in enumerate(predictions[:5]):
        print(f"  {i+1}. signal={p['ml_signal']:+.1f}, conf={p['ml_conf']:.3f} (xgb={p['xgb_conf']:.3f}, lstm={p['lstm_conf']:.3f})")
