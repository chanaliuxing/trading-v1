# brain/prompt_selector.py
"""
Prompt Selector: 基于历史数据选择最佳 prompt

功能:
- 查看历史 run 表现
- 选择表现最好的 execution_prompt_id
- 生成 BrainExecutionPlan
- 完全数据驱动,可审计
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from prompts.registry import PromptRegistry
from runs.history_index import RunHistoryIndex, PromptPerformanceSummary
from brain.plan import BrainExecutionPlan


@dataclass
class PromptSelectionContext:
    """Prompt 选择上下文"""
    symbol: str
    table: str
    start_ts: str
    end_ts: str
    label_id: str
    horizon: str
    base_prompt_id: str  # e.g., "v3_fast_market_following"
    since_days: int = 30
    min_runs_for_stats: int = 3
    default_allowed_model_types: Optional[List[str]] = None


class PromptSelector:
    """
    Prompt 选择器

    职责:
    - 在给定 base_prompt_id 下,查看历史 run 表现
    - 决定这次用哪个 execution_prompt_id
    - 生成 BrainExecutionPlan (只管计划,不管执行)

    策略:
    1. 如果有足够历史数据 (>= min_runs_for_stats),选择表现最好的
    2. 否则,fallback 到最新的 tactical variant
    3. 再不行,使用 base prompt 本身
    """

    def __init__(
        self,
        prompt_registry: PromptRegistry,
        history_index: RunHistoryIndex,
    ) -> None:
        self._prompts = prompt_registry
        self._history = history_index

    def make_execution_plan(self, ctx: PromptSelectionContext) -> BrainExecutionPlan:
        """
        生成执行计划

        Step 1: 看历史表现 → 选表现最好的 execution_prompt_id
        Step 2: 历史太少 → fallback 到 latest tactical variant
        Step 3: 无 tactical → fallback 到 base prompt
        Step 4: 填充 BrainExecutionPlan

        Args:
            ctx: 选择上下文

        Returns:
            BrainExecutionPlan
        """
        # 1) 尝试基于历史表现选 prompt
        perf_summaries = self._history.summarize_by_prompt(
            symbol=ctx.symbol,
            label_id=ctx.label_id,
            base_prompt_id=ctx.base_prompt_id,
            since_days=ctx.since_days,
        )

        execution_prompt_id: Optional[str] = None
        reason: str = ""

        # 检查是否有足够的历史数据
        if perf_summaries and len(perf_summaries) > 0:
            # 取表现最好的 (已按 metric_mean 降序排序)
            top = perf_summaries[0]

            # 只有当有足够样本时才信任
            if top.n_runs >= ctx.min_runs_for_stats:
                execution_prompt_id = top.prompt_id
                reason = (
                    f"Selected by history stats: prompt_id={top.prompt_id}, "
                    f"base={top.base_prompt_id}, "
                    f"n_runs={top.n_runs}, "
                    f"metric={top.metric_name}, "
                    f"mean={top.metric_mean:.3f}, "
                    f"median={top.metric_median:.3f}."
                )
            else:
                # 样本太少,不足以信任
                reason = (
                    f"Insufficient history for {top.prompt_id} "
                    f"(n_runs={top.n_runs} < {ctx.min_runs_for_stats}). "
                )

        # 2) 历史不足 → 使用最新 tactical variant
        if not execution_prompt_id:
            latest = self._prompts.latest_by_base(ctx.base_prompt_id)
            if latest:
                execution_prompt_id = latest.prompt_id
                reason += (
                    f"Fallback to latest_by_base: {latest.prompt_id} "
                    f"(created_at={latest.created_at})."
                )
            else:
                # 3) 无 tactical → 使用 base prompt 本身
                if self._prompts.exists(ctx.base_prompt_id):
                    execution_prompt_id = ctx.base_prompt_id
                    reason += (
                        f"No tactical prompts found for base_id={ctx.base_prompt_id}. "
                        f"Fallback to base prompt itself."
                    )
                else:
                    raise ValueError(
                        f"Neither tactical nor base prompt found for base_id={ctx.base_prompt_id}"
                    )

        # 4) 获取 meta 信息
        meta = self._prompts.get_meta(execution_prompt_id) if execution_prompt_id else None
        base_id = meta.base_id if meta and meta.base_id else ctx.base_prompt_id

        # 5) 默认允许的模型类型
        allowed_model_types = ctx.default_allowed_model_types or [
            "xgb",
            "lightgbm",
            "lasso",
            "elasticnet",
            "ridge",
            "logistic",
            "linear_svm",
        ]

        # 6) 构造 BrainExecutionPlan
        plan = BrainExecutionPlan(
            symbol=ctx.symbol,
            table=ctx.table,
            start_ts=ctx.start_ts,
            end_ts=ctx.end_ts,
            label_id=ctx.label_id,
            horizon=ctx.horizon,
            execution_prompt_id=execution_prompt_id,
            base_prompt_id=base_id,
            prompt_selection_reason=reason,
            allowed_model_types=allowed_model_types,
        )

        return plan

    def explain_selection(
        self,
        ctx: PromptSelectionContext,
    ) -> str:
        """
        解释为什么选择了某个 prompt (调试用)

        Returns:
            人类可读的选择原因说明
        """
        plan = self.make_execution_plan(ctx)

        lines = [
            "=== Prompt Selection Explanation ===",
            f"Symbol: {ctx.symbol}",
            f"Label: {ctx.label_id}",
            f"Base Prompt: {ctx.base_prompt_id}",
            f"",
            f"Selected: {plan.execution_prompt_id}",
            f"Reason: {plan.prompt_selection_reason}",
            f"",
            f"Allowed Models: {', '.join(plan.allowed_model_types)}",
        ]

        return "\n".join(lines)
