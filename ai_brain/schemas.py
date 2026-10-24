# ai_brain/schemas.py

"""
AI Brain Decision Schemas.

Pydantic schemas for structured AI Brain decisions. These schemas enforce:
- Limited action space (train_new_model, reuse_existing_model, skip_training, ask_human)
- Validated parameters (timestamps, label IDs, quantiles)
- Audit trail (run_id, rationale)

Based on upgradeFeatures15 specification.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, validator


class DatasetSanitySummary(BaseModel):
    """
    Compressed dataset sanity summary for LLM consumption.

    This is the output from DatasetSanityResult.brief_summary(),
    packaged for use in BrainContext.

    Attributes:
        ok: Overall pass/fail status
        severity: Severity level
        n_rows: Number of rows in dataset
        n_cols: Number of columns
        issue_counts: Count of issues by severity
        top_issues: Top 5 issues
        label_info: Optional label statistics
    """

    ok: bool
    severity: Literal["ok", "warning", "error"]
    n_rows: int
    n_cols: int
    issue_counts: Dict[str, int] = Field(default_factory=dict)
    top_issues: List[Dict[str, Any]] = Field(default_factory=list)
    label_info: Optional[Dict[str, Any]] = None


class BrainContext(BaseModel):
    """
    Research task context for AI Brain.

    Contains all information needed for the brain to make a decision,
    with controlled scope to prevent information overload.

    Attributes:
        run_id: Unique run ID for audit trail
        symbol: Trading symbol (e.g., NVDA, TSLA)
        timeframe: Bar frequency (e.g., '1m', '5s')
        start_ts: Training data start time (ISO format)
        end_ts: Training data end time (ISO format)
        available_labels: List of label IDs from LabelRegistry
        default_label_id: Recommended default label
        dataset_sanity: Optional dataset sanity summary
    """

    run_id: str = Field(
        ...,
        description="本次大脑调用的 run id，用来做审计/追踪",
    )
    symbol: str = Field(..., description="标的代码，如 NVDA / TSLA")
    timeframe: str = Field(..., description="bar 频率，如 '1m' 或 '5s'")
    start_ts: str = Field(..., description="训练数据起始时间（ISO 字符串）")
    end_ts: str = Field(..., description="训练数据结束时间（ISO 字符串）")

    available_labels: List[str] = Field(
        ...,
        description="LabelRegistry 里允许选择的 label_id 列表",
    )
    default_label_id: str = Field(
        ...,
        description="推荐的默认 label_id",
    )

    # 数据体检的结构化摘要（可选，如果已经跑过体检）
    dataset_sanity: Optional[DatasetSanitySummary] = None

    @validator("start_ts", "end_ts")
    def _validate_timestamp(cls, v: str) -> str:
        """Validate that timestamp is not empty."""
        if not v or not v.strip():
            raise ValueError("Timestamp cannot be empty")
        return v

    @validator("symbol")
    def _validate_symbol(cls, v: str) -> str:
        """Validate that symbol is uppercase and non-empty."""
        if not v or not v.strip():
            raise ValueError("Symbol cannot be empty")
        return v.upper()

    @validator("available_labels")
    def _validate_labels_not_empty(cls, v: List[str]) -> List[str]:
        """Validate that available_labels is not empty."""
        if not v:
            raise ValueError("available_labels cannot be empty")
        return v


class BrainPIParams(BaseModel):
    """
    Parameters for "train_new_model" action.

    If brain decides to train a new model, it must provide these parameters.
    All fields are validated to ensure safe execution.

    Attributes:
        table: ClickHouse table name
        symbol: Trading symbol (cross-checked with context)
        start_ts: Data start time for PI/training
        end_ts: Data end time for PI/training
        goal: High-level research goal
        label_id: Label ID (must be in available_labels)
        label_horizon_bars: Optional label horizon override
        risk_profile: Risk preference for strategy
        notes: Optional rationale for choices
    """

    table: str = Field(..., description="ClickHouse 表名，如 bars_1m")
    symbol: str = Field(..., description="标的代码（会再次校验）")
    start_ts: str = Field(..., description="用于 PI / 训练的数据起始时间")
    end_ts: str = Field(..., description="用于 PI / 训练的数据结束时间")

    goal: str = Field(
        ...,
        description="本次研究的高层目标说明，例如：'预测 10m forward return 的方向'",
    )

    label_id: str = Field(
        ...,
        description="要使用的 label id，必须来自 available_labels",
    )

    label_horizon_bars: Optional[int] = Field(
        None,
        description="可选，覆盖 LabelRegistry 中的 default_horizon_bars",
    )

    risk_profile: Literal["conservative", "balanced", "aggressive"] = Field(
        "balanced",
        description="大脑推荐的风险偏好（会影响后续 position sizing，不影响打标签）",
    )

    notes: Optional[str] = Field(
        None,
        description="大脑对选择此 label/区间的理由简述，方便审查",
    )

    @validator("start_ts", "end_ts", "table", "symbol", "goal", "label_id")
    def _not_empty(cls, v: str) -> str:
        """Validate that string fields are not empty."""
        if not v or not v.strip():
            raise ValueError(f"Field cannot be empty")
        return v

    @validator("label_horizon_bars")
    def _validate_horizon(cls, v: Optional[int]) -> Optional[int]:
        """Validate that label_horizon_bars is positive if provided."""
        if v is not None and v <= 0:
            raise ValueError("label_horizon_bars must be positive")
        return v


class BrainDecision(BaseModel):
    """
    AI Brain's unified decision structure.

    Orchestrator will enforce this schema as LLM's JSON output constraint.
    Brain cannot execute arbitrary SQL/Python - only select from allowed actions.

    Attributes:
        version: Schema version
        action: High-level action choice
        pi_params: Parameters for train_new_model (required if action="train_new_model")
        reuse_model_id: Model ID for reuse_existing_model
        rationale: Decision rationale (required, no code allowed)
    """

    version: str = Field(
        "brain_decision.v1",
        description="schema 版本号",
    )

    action: Literal[
        "train_new_model",  # 走 PI + HPO 流程
        "reuse_existing_model",  # 重用 / 微调已有模型
        "skip_training",  # 数据质量不好 / 信号弱 → 本轮不训练
        "ask_human",  # 明确请求人类介入
    ] = Field(
        ...,
        description="大脑本轮的高层行动选择",
    )

    # 只有在 action = "train_new_model" 时才需要填写
    pi_params: Optional[BrainPIParams] = Field(
        None,
        description="训练新模型所需的参数",
    )

    # 仅在 action = "reuse_existing_model" 时生效（简单占位，未来扩展）
    reuse_model_id: Optional[str] = Field(
        None,
        description="要重用的模型 ID（未来从模型注册表中选择）",
    )

    rationale: str = Field(
        ...,
        description="大脑做出这个决定的简短理由，方便审计；禁止包含任何代码/SQL",
    )

    @validator("rationale")
    def _validate_rationale(cls, v: str) -> str:
        """Validate that rationale is non-empty and doesn't contain code."""
        if not v or not v.strip():
            raise ValueError("Rationale cannot be empty")

        # Basic check for code-like patterns (not foolproof, but catches obvious cases)
        forbidden_patterns = [
            "IMPORT ",
            "EXEC(",
            "EVAL(",
            "SELECT ",
            "DROP ",
            "INSERT ",
            "DELETE ",
            "UPDATE ",
            "OS.",
            "SYSTEM(",
        ]
        v_upper = v.upper()
        for pattern in forbidden_patterns:
            if pattern in v_upper:
                raise ValueError(f"Rationale cannot contain code-like pattern: {pattern}")

        return v

    @validator("pi_params", always=True)
    def _validate_pi_params_for_action(cls, v: Optional[BrainPIParams], values: Dict[str, Any]) -> Optional[BrainPIParams]:
        """Validate that pi_params is provided if action="train_new_model"."""
        action = values.get("action")
        if action == "train_new_model" and v is None:
            raise ValueError("pi_params is required when action='train_new_model'")
        if action != "train_new_model" and v is not None:
            # Warning: pi_params provided but action is not train_new_model
            # We'll allow it but it won't be used
            pass
        return v

    def require_pi_params(self) -> BrainPIParams:
        """
        Safe helper method to get pi_params.

        Returns:
            BrainPIParams if action is train_new_model

        Raises:
            ValueError: If action is not train_new_model or pi_params is None
        """
        if self.action != "train_new_model" or self.pi_params is None:
            raise ValueError("BrainDecision does not contain valid pi_params.")
        return self.pi_params
