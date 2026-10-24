# pi/schemas.py

"""
PI Output Contract Schemas.

This module defines the contract for LLM outputs from the Principal Investigator.
All LLM-generated training plans must conform to these Pydantic schemas.

Key Security Features:
- No arbitrary SQL expressions allowed
- Feature selection via whitelist only
- Label selection from LabelRegistry only
- Model pool with validated parameters
- Decision rules with range validation

Based on upgradeFeatures14 specification.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, validator


class DerivedFeatureRef(BaseModel):
    """
    Reference to a derived feature from FeatureRegistry.

    LLM can only reference features by ID from the registry,
    not write arbitrary SQL expressions.

    Attributes:
        id: Feature ID from FeatureRegistry.derived
        alias: Optional column name in training data (defaults to registry output_name)
    """

    id: str = Field(..., description="FeatureRegistry.derived feature id")
    alias: Optional[str] = Field(
        None,
        description="Column name in training data (if None, uses registry default)",
    )


class FeatureRecipeV1(BaseModel):
    """
    Feature selection recipe for training.

    Defines which table to use and which features to select.
    All features must come from whitelisted sources.

    Attributes:
        use_table: Optional table name (if None, uses caller-provided table)
        base_features: List of base column names (must exist in ClickHouse + FeatureRegistry)
        derived: List of derived feature references (by ID only, no SQL)
        notes: Optional human-readable notes for debugging
    """

    use_table: Optional[str] = Field(
        None,
        description="Table name (if None, uses caller-provided table)",
    )
    base_features: List[str] = Field(
        default_factory=list,
        description="Base columns from ClickHouse table (must be in FeatureRegistry.columns)",
    )
    derived: List[DerivedFeatureRef] = Field(
        default_factory=list,
        description="Derived features by ID (no SQL expressions allowed)",
    )
    notes: Optional[str] = Field(
        None,
        description="Human-readable notes for debugging/review",
    )


class DecisionQuantileConfig(BaseModel):
    """
    Decision rule configuration for converting regression outputs to trading signals.

    Currently supports quantile-based decisions:
    - Prediction > q_long quantile → LONG
    - Prediction < q_short quantile → SHORT
    - Otherwise → FLAT

    Attributes:
        mode: Decision mode (currently only "quantile" supported)
        q_long: Quantile threshold for LONG signal (0 < q_long < 1)
        q_short: Quantile threshold for SHORT signal (0 < q_short < 1)
    """

    mode: Literal["quantile"] = "quantile"
    q_short: float = 0.3  # Declare q_short first so it's available when validating q_long
    q_long: float = 0.7

    @validator("q_long", "q_short")
    def _check_range(cls, v: float) -> float:
        """Validate that quantiles are in (0, 1) range."""
        if not 0.0 < v < 1.0:
            raise ValueError("q_long / q_short must be in (0, 1) range")
        return v

    @validator("q_long")
    def _check_order(cls, v: float, values: Dict[str, Any]) -> float:
        """Validate that q_short < q_long."""
        q_short = values.get("q_short")
        if q_short is not None and not (q_short < v):
            raise ValueError("q_short must be < q_long")
        return v


class ModelCandidate(BaseModel):
    """
    Single model candidate for HPO grid.

    Attributes:
        model_type: Model type string (e.g., 'xgb', 'lasso', 'ridge', 'lightgbm')
        model_params: Hyperparameter dictionary for this model type
    """

    model_type: str = Field(
        ...,
        description="Model type (e.g., 'xgb', 'lasso', 'ridge', 'lightgbm')",
    )
    model_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Hyperparameters for this model (empty dict uses defaults)",
    )


class TrainingPlanV1(BaseModel):
    """
    Training plan configuration for HPO/training.

    Defines task type, label, model pool, and decision rules.
    All fields must conform to strict schemas.

    Attributes:
        symbol: Trading symbol (e.g., 'NVDA', 'AAPL')
        task: Task type ('reg' for regression, 'clf' for classification)
        label_id: Label ID from LabelRegistry (e.g., 'ret_10m_fwd', 'tb_10_20_10')
        label_horizon_bars: Optional override for label horizon (uses registry default if None)
        decision: Decision rule configuration
        model_candidates: List of model candidates for HPO grid
        notes: Optional human-readable notes
    """

    symbol: str = Field(..., description="Trading symbol (e.g., 'NVDA')")
    task: Literal["reg", "clf"] = "reg"

    # Label must come from LabelRegistry (no arbitrary labels allowed)
    label_id: str = Field(
        ...,
        description="Label ID from LabelRegistry (e.g., 'ret_10m_fwd', 'tb_10_20_10')",
    )

    # Optional horizon override
    label_horizon_bars: Optional[int] = Field(
        None,
        description="Override label horizon (if None, uses LabelRegistry default)",
    )

    # Decision rules
    decision: DecisionQuantileConfig = Field(
        default_factory=DecisionQuantileConfig,
        description="Decision rule for converting predictions to signals",
    )

    # Model pool
    model_candidates: List[ModelCandidate] = Field(
        default_factory=list,
        description="Model pool for HPO (if empty, uses default xgb)",
    )

    notes: Optional[str] = Field(
        None,
        description="Strategy intent / special constraints (human-readable)",
    )

    @validator("model_candidates", always=True)
    def _ensure_at_least_one_model(cls, v: List[ModelCandidate]) -> List[ModelCandidate]:
        """
        Ensure at least one model candidate exists.

        If list is empty, adds a conservative default XGBoost model.
        """
        if not v:
            # Add conservative default XGBoost
            return [
                ModelCandidate(
                    model_type="xgb",
                    model_params={
                        "max_depth": 5,
                        "n_estimators": 200,
                        "learning_rate": 0.05,
                        "subsample": 0.8,
                        "colsample_bytree": 0.8,
                    },
                )
            ]
        return v


class PIPlanV1(BaseModel):
    """
    Top-level contract for LLM output from Principal Investigator.

    This is the root object that LLM must produce.
    All fields are validated by Pydantic schemas.

    Attributes:
        version: Contract version (must be "pi_plan_v1")
        feature_recipe: Feature selection recipe
        training_plan: Training configuration plan

    Example LLM output:
        {
            "version": "pi_plan_v1",
            "feature_recipe": {
                "base_features": ["close", "volume", "vwap"],
                "derived": [
                    {"id": "ret_1", "alias": "ret_1"},
                    {"id": "rv_20", "alias": "rv_20"}
                ]
            },
            "training_plan": {
                "symbol": "NVDA",
                "task": "reg",
                "label_id": "ret_10m_fwd",
                "decision": {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
                "model_candidates": [
                    {
                        "model_type": "xgb",
                        "model_params": {"max_depth": 5, "n_estimators": 400}
                    }
                ]
            }
        }
    """

    version: Literal["pi_plan_v1"] = "pi_plan_v1"
    feature_recipe: FeatureRecipeV1
    training_plan: TrainingPlanV1
