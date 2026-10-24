# brain/decision_schema.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, List


ALLOWED_DECISIONS = {"HOLD", "ENTER", "EXIT", "ADJUST"}
ALLOWED_SIDES = {"LONG", "SHORT"}


@dataclass
class BrainDecision:
    """
    执行大脑返回的一条决策。
    这是**唯一**允许大脑输出的 JSON 结构。
    """
    decision: str                 # "HOLD" | "ENTER" | "EXIT" | "ADJUST"
    chosen_run_id: Optional[str]  # 当 decision != HOLD 时，必须在 allowed_run_ids 中
    side: Optional[str]           # "LONG" | "SHORT"（ENTER/ADJUST 时用）
    size_frac: float              # 0.0 ~ 1.0（占可用风险/仓位的比例）
    notes: Optional[str] = None   # 简短说明，方便日志与 debug

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "chosen_run_id": self.chosen_run_id,
            "side": self.side,
            "size_frac": self.size_frac,
            "notes": self.notes,
        }


class BrainDecisionValidationError(Exception):
    """大脑输出格式或内容不合法，直接触发 HOLD。"""


def validate_brain_decision(
    raw: Dict[str, Any],
    allowed_run_ids: List[str],
) -> BrainDecision:
    """
    对 LLM 返回的 dict 做强校验：
    - decision 必须在白名单内
    - size_frac 必须在 [0, 1]
    - chosen_run_id 必须在 allowed_run_ids 中（除非 HOLD）
    - side 只能是 LONG/SHORT 或 None
    校验失败 → 抛异常，在外层统一降级为 HOLD。
    """
    if not isinstance(raw, dict):
        raise BrainDecisionValidationError("Brain output is not a JSON object")

    decision = str(raw.get("decision", "")).upper()
    if decision not in ALLOWED_DECISIONS:
        raise BrainDecisionValidationError(f"Invalid decision: {decision}")

    chosen_run_id = raw.get("chosen_run_id")
    if decision != "HOLD":
        if not chosen_run_id or chosen_run_id not in allowed_run_ids:
            raise BrainDecisionValidationError(
                f"chosen_run_id {chosen_run_id!r} not in allowed_run_ids"
            )
    else:
        chosen_run_id = None

    side = raw.get("side")
    if side is not None:
        side = str(side).upper()
        if side not in ALLOWED_SIDES:
            raise BrainDecisionValidationError(f"Invalid side: {side}")
    # HOLD/EXIT 时，side 可以为 None

    size_frac = raw.get("size_frac", 0.0)
    try:
        size_frac = float(size_frac)
    except (TypeError, ValueError):
        raise BrainDecisionValidationError(f"size_frac not a number: {size_frac!r}")
    if not (0.0 <= size_frac <= 1.0):
        raise BrainDecisionValidationError(f"size_frac out of range [0,1]: {size_frac}")

    notes = raw.get("notes")
    if notes is not None:
        notes = str(notes)

    return BrainDecision(
        decision=decision,
        chosen_run_id=chosen_run_id,
        side=side,
        size_frac=size_frac,
        notes=notes,
    )
