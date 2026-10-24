# brain/plan.py
"""
Brain Execution Plan: AI 大脑的执行处方单

定义 AI 大脑下达训练任务的标准格式,包括:
- 目标资产和时间窗口
- 标签设置 (必须来自 LabelRegistry)
- Prompt 选择和原因
- 模型搜索空间约束
- 数据质量要求

所有内容可审计、可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class BrainExecutionPlan:
    """
    AI 大脑下达的一次"训练任务处方单"

    - 只包含可执行的白名单字段
    - 所有内容都可写入 run metadata,后续可审计
    - 防止 AI 直接修改 SQL/超参/标签定义
    """

    # === 目标资产 & 时间窗口 ===
    symbol: str
    table: str
    start_ts: str
    end_ts: str

    # === 标签设置（必须来自 LabelRegistry） ===
    label_id: str
    horizon: str  # e.g. "10b", "30b" (bars)

    # === Prompt 信息（治理关键） ===
    execution_prompt_id: str
    base_prompt_id: Optional[str] = None
    prompt_selection_reason: str = ""

    # === 模型和搜索空间约束 ===
    allowed_model_types: List[str] = field(default_factory=list)
    max_trials: int = 30

    # === 数据质量 & 体检相关策略 ===
    require_clean_dataset: bool = True
    min_rows: int = 5_000
    max_missing_ratio: float = 0.5  # 特征缺失超过此比例则剔除

    # === 其他策略风格标签 ===
    mode: str = "training"  # 未来可有 "live_eval" 等
    notes: str = ""

    # === 额外配置（必须在执行侧重新白名单过滤） ===
    extra_cfg: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典,方便序列化"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrainExecutionPlan":
        """从字典恢复"""
        return cls(**data)

    def validate(self) -> List[str]:
        """
        验证 plan 的合法性

        Returns:
            错误列表 (空列表表示通过)
        """
        errors: List[str] = []

        # 必填字段检查
        if not self.symbol:
            errors.append("symbol is required")
        if not self.table:
            errors.append("table is required")
        if not self.label_id:
            errors.append("label_id is required")
        if not self.execution_prompt_id:
            errors.append("execution_prompt_id is required")

        # 时间范围检查
        if not self.start_ts or not self.end_ts:
            errors.append("start_ts and end_ts are required")

        # 数值范围检查
        if self.max_trials <= 0:
            errors.append("max_trials must be > 0")
        if self.min_rows <= 0:
            errors.append("min_rows must be > 0")
        if not (0 <= self.max_missing_ratio <= 1):
            errors.append("max_missing_ratio must be in [0, 1]")

        # 模式检查
        if self.mode not in ["training", "live_eval", "backtest"]:
            errors.append(f"Unknown mode: {self.mode}")

        return errors
