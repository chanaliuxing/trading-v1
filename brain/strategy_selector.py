# brain/strategy_selector.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from experiments.summary import build_experiment_summary_for_llm
from experiments.registry import load_experiment_records, DEFAULT_INDEX_PATH
from brain.context_builder import build_brain_prompts
from brain.decision_schema import (
    BrainDecision,
    BrainDecisionValidationError,
    validate_brain_decision,
)
from brain.prompt_library import PromptLibrary
from features.registry import FeatureRegistry
from llm.llm_client import LLMClient


class StrategySelector:
    """
    让 AI 大脑在给定的历史实验中选择一个 run_id + 仓位决策。
    """

    def __init__(
        self,
        llm: LLMClient,
        prompt_lib: PromptLibrary,
        prompt_id: str,
        *,
        index_path: Path = DEFAULT_INDEX_PATH,
    ) -> None:
        self._llm = llm
        self._prompt_lib = prompt_lib
        self._prompt_id = prompt_id
        self._index_path = index_path

    def select(
        self,
        *,
        symbol: str,
        now_ts: str,
        freq: str,
        horizon: str,
        label_id: str,
        market_snapshot: Dict[str, Any],
        risk_gates: Dict[str, Any],
        dataset_sanity: Dict[str, Any],
        registry: FeatureRegistry,
    ) -> BrainDecision:
        """
        核心入口：
        - 从 experiment index 里找出 (symbol, freq, horizon, label_id) 的所有实验
        - 构建 prompt，告诉大脑：你只能在这些 run_id 里选
        - 解析 + 校验大脑输出
        - 任意错误 → HOLD
        """
        # 1) load experiments for filter
        records = load_experiment_records(
            self._index_path,
            symbol=symbol,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
        )
        allowed_run_ids = [r.run_id for r in records]
        if not allowed_run_ids:
            # 没有历史实验 → 强制 HOLD
            return BrainDecision(
                decision="HOLD",
                chosen_run_id=None,
                side=None,
                size_frac=0.0,
                notes="No historical experiments found; forced HOLD.",
            )

        # 2) build experiment summary for LLM
        experiment_summary = build_experiment_summary_for_llm(
            symbol=symbol,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
            index_path=self._index_path,
        )

        # 3) build full prompts (system + user)
        ctx = build_brain_prompts(
            prompt_lib=self._prompt_lib,
            prompt_id=self._prompt_id,
            symbol=symbol,
            now_ts=now_ts,
            freq=freq,
            horizon=horizon,
            market_snapshot=market_snapshot,
            risk_gates=risk_gates,
            dataset_sanity=dataset_sanity,
            experiment_summary=experiment_summary,
            registry=registry,
            # 额外附加：允许的 run_id 列表
            allowed_run_ids=allowed_run_ids,
        )

        # 4) call LLM
        raw = self._llm.generate(
            system_prompt=ctx["system_prompt"],
            user_prompt=ctx["user_prompt"],
        )

        # 假定 LLMClient 已经返回了 dict（你之前的 PI 实现就是这样用的）
        # 如果返回的是 str/JSON，需要在这里做 json.loads(raw)。
        if isinstance(raw, str):
            import json

            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                return BrainDecision(
                    decision="HOLD",
                    chosen_run_id=None,
                    side=None,
                    size_frac=0.0,
                    notes=f"Brain returned invalid JSON, downgraded to HOLD: {e}",
                )

        # 5) validate
        try:
            decision = validate_brain_decision(raw, allowed_run_ids=allowed_run_ids)
        except BrainDecisionValidationError as e:
            # 任意不合规 → HOLD
            return BrainDecision(
                decision="HOLD",
                chosen_run_id=None,
                side=None,
                size_frac=0.0,
                notes=f"Brain decision invalid, downgraded to HOLD: {e}",
            )

        return decision
