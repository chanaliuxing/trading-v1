# feature_validator.py
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
from typing import Dict, Any, List

ALLOWED_OPS = {
    "log1p","clip","power","sign","rank_pct",
    "lag","diff","rolling_sum","rolling_mean","rolling_std","rolling_zscore","ema",
    "ratio","ratio_minus_1","sum","product","min","max",
    "vwap_distance","effective_spread","amihud_illiquidity","price_impact_hl",
    "ta.rsi","ta.bbands","ta.macd","ta.atr","ta.obv"
}

FORBIDDEN_NAMES = {"y", "label", "target", "forward", "future"}

def validate_program(program: Dict[str, Any], *, max_steps: int = 12) -> List[str]:
    """返回警告列表；违反规则则抛异常。"""
    warns: List[str] = []
    steps = program.get("steps") or []
    if not isinstance(steps, list) or len(steps) == 0:
        raise ValueError("Program must have non-empty 'steps' list.")
    if len(steps) > max_steps:
        raise ValueError(f"Program exceeds max steps: {len(steps)} > {max_steps}")

    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise ValueError(f"Step #{i} must be dict.")
        name = str(st.get("name", f"feat_{i}")).lower()
        for bad in FORBIDDEN_NAMES:
            if bad in name:
                raise ValueError(f"Step #{i} name contains forbidden token `{bad}`.")
        op = st.get("op")
        if op not in ALLOWED_OPS:
            raise ValueError(f"Step #{i} uses unknown op `{op}`.")
        if op in {"lag", "diff"} and int(st.get("lags", st.get("periods", 1))) < 1:
            raise ValueError(f"Step #{i} {op} requires lags/periods >= 1")
        if "window_bars" in st and int(st["window_bars"]) < 1:
            raise ValueError(f"Step #{i} window_bars must be >= 1")

    return warns
