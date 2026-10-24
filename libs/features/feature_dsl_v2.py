# feature_dsl_v2.py
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
from libs.features.feature_ops import get_op

class FeatureExecutionError(Exception):
    pass

def _safe_name(name: str) -> str:
    return name.replace(" ", "_").replace("-", "_")

def apply_program(df: pd.DataFrame, program: Dict[str, Any], *,
                  allow_overwrite: bool = False) -> Tuple[pd.DataFrame, List[str]]:
    """
    在 df 上按步骤执行 program（就地增列，零 deepcopy）。
    program: {"steps": [{"name": "...", "op": "rolling_std", "col": "ret_1", "window_bars": 20, "per_symbol": true}, ...]}
    返回: (df, 新增列名列表)
    """
    if "steps" not in program or not isinstance(program["steps"], list):
        raise FeatureExecutionError("Program must include 'steps' as a list.")

    new_cols: List[str] = []
    for i, step in enumerate(program["steps"]):
        if not isinstance(step, dict):
            raise FeatureExecutionError(f"Step #{i} must be dict.")
        name = _safe_name(step.get("name", f"feat_{i}"))
        op_name = step.get("op")
        if not op_name:
            raise FeatureExecutionError(f"Step #{i}: missing 'op'.")

        if (name in df.columns) and not allow_overwrite:
            raise FeatureExecutionError(f"Column '{name}' already exists; set allow_overwrite=True to override.")

        op = get_op(op_name)
        # 运行算子
        out = op(df, **{k: v for k, v in step.items() if k not in ("name", "op")})

        # 支持 DataFrame 返回（如 MACD 返回 3 列）
        if isinstance(out, pd.DataFrame):
            for c in out.columns:
                colname = _safe_name(f"{name}_{c}")
                df[colname] = out[c].astype("float32")
                new_cols.append(colname)
        else:
            df[name] = out.astype("float32")
            new_cols.append(name)

    return df, new_cols

def build_features(df: pd.DataFrame, programs: Dict[str, Dict[str, Any]],
                   program_id: str, *, allow_overwrite: bool = False) -> Tuple[pd.DataFrame, List[str]]:
    if program_id not in programs:
        raise KeyError(f"Unknown feature program id: {program_id}")
    return apply_program(df, programs[program_id], allow_overwrite=allow_overwrite)
