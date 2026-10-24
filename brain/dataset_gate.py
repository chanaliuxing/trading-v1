# brain/dataset_gate.py

from __future__ import annotations

import json
from typing import Any, Dict

from llm.llm_client import LLMClient
from brain.contracts import BrainDatasetContext, BrainTrainingDecision


class DatasetGate:
    """
    AI 大脑的"数据门卫"：
    - 把 BrainDatasetContext 打包成 JSON
    - 调用 LLM
    - 解析返回，输出 BrainTrainingDecision

    This class acts as the AI brain's gatekeeper for training decisions.
    It reviews dataset quality reports and makes informed decisions about
    whether to proceed with training.

    Design philosophy:
    - Risk-first: Prefer to skip/abort rather than train on bad data
    - Transparent: Provide clear reasons for all decisions
    - Adjustable: Can modify HPO parameters based on data characteristics
    """

    def __init__(self, llm_cfg: Dict[str, Any]) -> None:
        """
        Initialize dataset gate with LLM configuration.

        Args:
            llm_cfg: LLM configuration dict with model, base_url, etc.
        """
        self._llm = LLMClient(
            model=llm_cfg.get("model"),
            base_url=llm_cfg.get("base_url"),
        )
        # System prompt emphasizes risk-first decision making
        self._system_prompt = (
            "You are the head of quantitative research for an intraday trading platform. "
            "You are STRICTLY risk-first: you must refuse to train models on corrupted, "
            "low-quality, or mislabeled data.\n\n"
            "You will receive a JSON manifest describing a candidate dataset and HPO grid. "
            "Your task is to decide:\n"
            "- whether the dataset is safe enough for training/backtest,\n"
            "- whether the HPO grid needs adjustments,\n"
            "- or whether to skip / abort.\n\n"
            "You MUST output valid JSON only, with no extra text."
        )

    def decide(self, ctx: BrainDatasetContext) -> BrainTrainingDecision:
        """
        Make training decision based on dataset context.

        Args:
            ctx: Dataset context with manifest and HPO grid

        Returns:
            BrainTrainingDecision with action and reasoning

        Decision rules (encoded in prompt):
        1. If dataset_sanity.ok == false OR label_sanity.ok == false → abort (error)
        2. If feature_sanity.ok == false with small bad_features → adjust_hpo (drop features)
        3. If severe label imbalance → train with warning (require proper metrics)
        4. When borderline quality → prefer skip over train
        """
        ctx_json = json.dumps(
            {
                "symbol": ctx.symbol,
                "table": ctx.table,
                "freq": ctx.freq,
                "goal": ctx.goal,
                "horizon": ctx.horizon,
                "dataset_manifest": ctx.dataset_manifest,
                "hpo_grid": ctx.hpo_grid,
                "last_experiments": ctx.last_experiments or [],
            },
            ensure_ascii=False,
        )

        user_prompt = (
            "Here is the dataset context and manifest:\n\n"
            f"```json\n{ctx_json}\n```\n\n"
            "Follow these strict rules:\n"
            "1. If dataset_manifest.dataset_sanity.ok == false OR label_sanity.ok == false → "
            "action = \"abort\"; severity = \"error\".\n"
            "2. If feature_sanity.ok == false but only a small number of bad_features are present → "
            "you may choose action = \"adjust_hpo\" AND recommend dropping those bad features in adjusted_hpo_grid.feature_subset.\n"
            "3. If label_sanity.summary.class_imbalance is \"severe\" (e.g., positive class < 1%) → "
            "you may still train, but must set severity = \"warning\" and note that proper metrics (AUC/F1/PR) "
            "and rebalancing are required.\n"
            "4. When in doubt (quality is borderline), prefer action = \"skip\" instead of \"train\".\n\n"
            "Output JSON ONLY with this schema:\n"
            "{\n"
            '  "action": "train" | "skip" | "adjust_hpo" | "abort",\n'
            '  "reasons": "short explanation in English",\n'
            '  "severity": "info" | "warning" | "error",\n'
            '  "adjusted_hpo_grid": null OR {...},\n'
            '  "notes": {...}\n'
            "}"
        )

        raw = self._llm.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        # Parse LLM response (handle both string and dict responses)
        if isinstance(raw, str):
            resp = json.loads(raw)
        else:
            resp = raw

        return BrainTrainingDecision(
            action=resp.get("action", "abort"),
            reasons=resp.get("reasons", ""),
            severity=resp.get("severity", "info"),
            adjusted_hpo_grid=resp.get("adjusted_hpo_grid"),
            notes=resp.get("notes") or {},
        )
