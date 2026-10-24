"""ML services package.

IMPORTANT: This package now uses REAL ML inference by default.
Mock service is only for testing purposes.
"""

from .real_inference_service import RealMLInferenceService, get_real_service
from .model_inference_service import ModelMLInferenceService, get_model_service

# Keep mock for testing only
from .mock_inference_service import MockMLInferenceService, get_mock_service, inject_mock_predictions

# Default to MODEL service (loads trained model from disk)
get_service = get_model_service
MLInferenceService = ModelMLInferenceService

__all__ = [
    # PRIMARY (MODEL-BASED) INFERENCE
    "ModelMLInferenceService",
    "get_model_service",
    "get_service",           # Alias for get_model_service
    "MLInferenceService",    # Alias for ModelMLInferenceService

    # ALTERNATIVE (OOF-BASED) INFERENCE
    "RealMLInferenceService",
    "get_real_service",

    # LEGACY/TEST ONLY - DO NOT USE IN PRODUCTION
    "MockMLInferenceService",
    "get_mock_service",
    "inject_mock_predictions",
]
