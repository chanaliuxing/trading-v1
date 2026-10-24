#!/usr/bin/env python3
from __future__ import annotations

"""
Outlier detector for realtime ML features.

Stateless mathematical tool: given a fitted RobustScaler and a single
feature vector, compute a simple robust z-score profile and flag
samples whose feature pattern deviates too much from the training
distribution.
"""

from typing import Dict, List, Any

import numpy as np
from sklearn.preprocessing import RobustScaler  # type: ignore

try:
    from libs.logger import get_logger

    logger = get_logger(__name__)
except Exception:  # pragma: no cover - fallback for ad‑hoc runs
    import logging

    logger = logging.getLogger(__name__)


class OutlierDetector:
    """
    Real-time Outlier Detector (Stateless Tool).

    Uses the center_/scale_ statistics from a fitted RobustScaler to
    compute per-feature robust z-scores, and derives an overall
    outlier score from the fraction of features exceeding a threshold.
    """

    def __init__(self, scaler: RobustScaler, feature_cols: List[str]):
        self.scaler = scaler
        self.feature_cols = feature_cols

        n = len(feature_cols)
        # Center (typically median) and scale (IQR) from RobustScaler
        if hasattr(scaler, "center_"):
            center = getattr(scaler, "center_")
        else:
            center = np.zeros(n, dtype=float)

        if hasattr(scaler, "scale_"):
            scale = getattr(scaler, "scale_")
        else:
            scale = np.ones(n, dtype=float)

        center = np.asarray(center, dtype=float).reshape(-1)
        scale = np.asarray(scale, dtype=float).reshape(-1)
        if center.shape[0] != n:
            center = np.resize(center, n)
        if scale.shape[0] != n:
            scale = np.resize(scale, n)

        # Guard against zero scale to avoid division by zero
        scale = np.where(scale == 0.0, 1.0, scale)

        self.center = center
        self.scale = scale

    def check(
        self,
        vector: np.ndarray,
        z_threshold: float = 10.0,
        ratio_limit: float = 0.3,
    ) -> Dict[str, Any]:
        """
        Check whether the feature vector is an outlier relative to the
        training distribution captured by the scaler.

        Args:
            vector: Raw feature vector (same ordering as feature_cols).
            z_threshold: Per-feature robust z-score threshold.
            ratio_limit: Fraction of features above z_threshold needed
                         to flag the sample as an outlier.
        """
        vec = np.asarray(vector, dtype=float).reshape(-1)
        if vec.shape[0] != self.center.shape[0]:
            # Dimension mismatch – fail open but log for diagnosis.
            logger.warning(
                "outlier_dim_mismatch",
                vec_len=int(vec.shape[0]),
                center_len=int(self.center.shape[0]),
            )
            return {
                "is_outlier": False,
                "outlier_score": 0.0,
                "max_z": 0.0,
                "details": [],
            }

        z_scores = np.abs((vec - self.center) / self.scale)
        outliers = z_scores > float(z_threshold)
        n_outliers = int(outliers.sum())
        ratio = float(n_outliers / max(len(vec), 1))

        is_abnormal = ratio > float(ratio_limit)

        result: Dict[str, Any] = {
            "is_outlier": is_abnormal,
            "outlier_score": ratio,
            "max_z": float(z_scores.max()) if z_scores.size else 0.0,
            "details": [],
        }

        if is_abnormal:
            # Log the top-3 most extreme features for debugging.
            top_idx = np.argsort(z_scores)[-3:][::-1]
            details: List[str] = []
            for i in top_idx:
                if z_scores[i] > float(z_threshold):
                    fname = self.feature_cols[i] if i < len(self.feature_cols) else f"idx_{i}"
                    details.append(f"{fname}(z={z_scores[i]:.1f})")
            result["details"] = details

        return result

