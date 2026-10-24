#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feature DSL (Domain-Specific Language) Engine

AI大脑通过修改JSON来控制特征工程,不直接写Python代码。

设计理念:
- AI只修改 feature_programs.json
- Python执行预定义的安全算子
- 训练/回测/实盘使用完全相同的特征计算逻辑

Author: Trading Team
Date: 2025-11-12
"""

import json
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Tuple, Optional
from pathlib import Path


class FeatureProgramRunner:
    """
    特征程序执行引擎

    职责:
    1. 加载 feature_programs.json
    2. 按步骤执行算子(operators)
    3. 返回增强后的DataFrame + 特征列名 + 元数据
    """

    def __init__(self, config_path: str = "feature_programs.json"):
        """
        初始化特征程序运行器

        Args:
            config_path: feature_programs.json 的路径
        """
        self.config_path = config_path
        self.programs = self._load_programs()

    def _load_programs(self) -> Dict[str, Any]:
        """加载特征程序配置"""
        try:
            with open(self.config_path, "r") as f:
                programs = json.load(f)
            print(f"[FeatureDSL] Loaded {len(programs)} programs from {self.config_path}")
            return programs
        except FileNotFoundError:
            print(f"[FeatureDSL] WARNING: {self.config_path} not found, using empty programs")
            return {}
        except json.JSONDecodeError as e:
            print(f"[FeatureDSL] ERROR: Invalid JSON in {self.config_path}: {e}")
            return {}

    def apply(
        self,
        df: pd.DataFrame,
        program_id: str,
        verbose: bool = False
    ) -> Tuple[pd.DataFrame, List[str], Dict[str, Any]]:
        """
        应用特征程序到DataFrame

        Args:
            df: 输入DataFrame (必须包含 'symbol' 和 'ts' 列)
            program_id: 特征程序ID (如 'baseline_v1', 'momentum_v1')
            verbose: 是否打印详细日志

        Returns:
            (df_enriched, new_feature_cols, meta)
            - df_enriched: 增强后的DataFrame (包含原始列 + 新特征列)
            - new_feature_cols: 新生成的特征列名列表
            - meta: 元数据字典 (program_id, description, steps_executed)
        """
        if program_id not in self.programs:
            if verbose:
                print(f"[FeatureDSL] Unknown program_id '{program_id}', returning original df")
            return df, [], {"program_id": program_id, "status": "unknown"}

        program = self.programs[program_id]
        steps = program.get("steps", [])

        if not steps:
            if verbose:
                print(f"[FeatureDSL] Program '{program_id}' has no steps")
            return df, [], {"program_id": program_id, "status": "empty"}

        if verbose:
            print(f"[FeatureDSL] Applying '{program_id}': {program.get('description', '')}")
            print(f"[FeatureDSL] {len(steps)} steps to execute")

        # 记录开始时的列
        original_cols = set(df.columns)

        # 逐步执行
        for i, step in enumerate(steps):
            feat_name = step.get("name")
            op = step.get("op")

            if not feat_name or not op:
                if verbose:
                    print(f"[FeatureDSL] Step {i}: missing 'name' or 'op', skipping")
                continue

            try:
                df = self._execute_operator(df, op, feat_name, step)
                if verbose:
                    print(f"[FeatureDSL] Step {i+1}/{len(steps)}: created '{feat_name}' using '{op}' ✓")
            except Exception as e:
                if verbose:
                    print(f"[FeatureDSL] Step {i}: error executing '{feat_name}' with '{op}': {e}")
                continue

        # 记录新生成的特征列
        new_cols = [c for c in df.columns if c not in original_cols]

        meta = {
            "program_id": program_id,
            "description": program.get("description", ""),
            "status": "success",
            "steps_total": len(steps),
            "features_created": len(new_cols)
        }

        if verbose:
            print(f"[FeatureDSL] Program '{program_id}' completed: {len(new_cols)} features created")

        return df, new_cols, meta

    def _execute_operator(
        self,
        df: pd.DataFrame,
        op: str,
        feat_name: str,
        step: Dict[str, Any]
    ) -> pd.DataFrame:
        """
        执行单个算子

        Args:
            df: DataFrame
            op: 算子名称
            feat_name: 生成的特征列名
            step: 算子参数字典

        Returns:
            增强后的DataFrame (原地修改 + 返回)
        """
        # ========== 比率运算 ==========
        if op == "ratio":
            num_col = step.get("num")
            den_col = step.get("den")
            eps = step.get("eps", 1e-6)
            if num_col in df.columns and den_col in df.columns:
                df[feat_name] = df[num_col] / (df[den_col] + eps)

        elif op == "ratio_minus_1":
            num_col = step.get("num")
            den_col = step.get("den")
            eps = step.get("eps", 1e-6)
            if num_col in df.columns and den_col in df.columns:
                df[feat_name] = (df[num_col] / (df[den_col] + eps)) - 1.0

        # ========== 对数变换 ==========
        elif op == "log1p":
            col = step.get("col")
            if col in df.columns:
                df[feat_name] = np.log1p(df[col].astype(float))

        # ========== 滚动聚合 ==========
        elif op == "rolling_sum":
            df = self._rolling_op(df, feat_name, step, "sum")

        elif op == "rolling_mean":
            df = self._rolling_op(df, feat_name, step, "mean")

        elif op == "rolling_std":
            df = self._rolling_op(df, feat_name, step, "std")

        elif op == "rolling_max":
            df = self._rolling_op(df, feat_name, step, "max")

        elif op == "rolling_min":
            df = self._rolling_op(df, feat_name, step, "min")

        elif op == "rolling_zscore":
            df = self._rolling_zscore(df, feat_name, step)

        # ========== 时序基本算子 ==========
        elif op == "lag":
            df = self._lag(df, feat_name, step)

        elif op == "diff":
            df = self._diff(df, feat_name, step)

        elif op == "ema":
            df = self._ema(df, feat_name, step)

        # ========== 非线性变换 ==========
        elif op == "abs":
            col = step.get("col")
            if col in df.columns:
                df[feat_name] = df[col].abs()

        elif op == "power":
            col = step.get("col")
            p = step.get("power", 2)
            if col in df.columns:
                df[feat_name] = df[col] ** p

        elif op == "clip":
            col = step.get("col")
            lower = step.get("lower", -np.inf)
            upper = step.get("upper", np.inf)
            if col in df.columns:
                df[feat_name] = df[col].clip(lower, upper)

        elif op == "sign":
            col = step.get("col")
            if col in df.columns:
                df[feat_name] = np.sign(df[col])

        # ========== 交互特征 ==========
        elif op == "product":
            col1 = step.get("col1")
            col2 = step.get("col2")
            if col1 in df.columns and col2 in df.columns:
                df[feat_name] = df[col1] * df[col2]

        elif op == "sum":
            col1 = step.get("col1")
            col2 = step.get("col2")
            if col1 in df.columns and col2 in df.columns:
                df[feat_name] = df[col1] + df[col2]

        # ========== 微观结构特征 ==========
        elif op == "price_impact_hl":
            df = self._price_impact_hl(df, feat_name, step)

        elif op == "effective_spread":
            eps = step.get("eps", 1e-6)
            if "close" in df.columns and "vwap" in df.columns:
                df[feat_name] = 2 * np.abs(df["close"] - df["vwap"]) / (df["vwap"] + eps)

        elif op == "amihud_illiquidity":
            eps = step.get("eps", 1e-9)
            if "ret_1" in df.columns and "volume" in df.columns and "close" in df.columns:
                df[feat_name] = np.abs(df["ret_1"]) / ((df["volume"] * df["close"]) + eps)

        else:
            raise ValueError(f"Unknown operator: {op}")

        return df

    # ========== 辅助方法 ==========

    def _rolling_op(
        self,
        df: pd.DataFrame,
        feat_name: str,
        step: Dict[str, Any],
        agg_func: str
    ) -> pd.DataFrame:
        """通用滚动聚合算子"""
        col = step.get("col")
        window = step.get("window_bars", 10)
        min_periods = step.get("min_periods", 1)
        per_symbol = step.get("per_symbol", True)

        if col not in df.columns:
            return df

        if per_symbol and "symbol" in df.columns:
            grouped = df.groupby("symbol", sort=False)[col]
            rolling = grouped.rolling(window=window, min_periods=min_periods)

            if agg_func == "sum":
                result = rolling.sum()
            elif agg_func == "mean":
                result = rolling.mean()
            elif agg_func == "std":
                result = rolling.std()
            elif agg_func == "max":
                result = rolling.max()
            elif agg_func == "min":
                result = rolling.min()
            else:
                raise ValueError(f"Unknown agg_func: {agg_func}")

            df[feat_name] = result.reset_index(level=0, drop=True)
        else:
            rolling = df[col].rolling(window=window, min_periods=min_periods)

            if agg_func == "sum":
                df[feat_name] = rolling.sum()
            elif agg_func == "mean":
                df[feat_name] = rolling.mean()
            elif agg_func == "std":
                df[feat_name] = rolling.std()
            elif agg_func == "max":
                df[feat_name] = rolling.max()
            elif agg_func == "min":
                df[feat_name] = rolling.min()

        return df

    def _rolling_zscore(
        self,
        df: pd.DataFrame,
        feat_name: str,
        step: Dict[str, Any]
    ) -> pd.DataFrame:
        """滚动Z分数"""
        col = step.get("col")
        window = step.get("window_bars", 50)
        min_periods = step.get("min_periods", 10)
        per_symbol = step.get("per_symbol", True)

        if col not in df.columns:
            return df

        if per_symbol and "symbol" in df.columns:
            grouped = df.groupby("symbol", sort=False)[col]
            roll_mean = grouped.rolling(window=window, min_periods=min_periods).mean().reset_index(level=0, drop=True)
            roll_std = grouped.rolling(window=window, min_periods=min_periods).std().reset_index(level=0, drop=True).replace(0.0, 1.0)
            df[feat_name] = (df[col] - roll_mean) / roll_std
        else:
            roll_mean = df[col].rolling(window=window, min_periods=min_periods).mean()
            roll_std = df[col].rolling(window=window, min_periods=min_periods).std().replace(0.0, 1.0)
            df[feat_name] = (df[col] - roll_mean) / roll_std

        return df

    def _lag(
        self,
        df: pd.DataFrame,
        feat_name: str,
        step: Dict[str, Any]
    ) -> pd.DataFrame:
        """滞后特征"""
        col = step.get("col")
        lags = step.get("lags", 1)
        per_symbol = step.get("per_symbol", True)

        if col not in df.columns:
            return df

        if per_symbol and "symbol" in df.columns:
            df[feat_name] = df.groupby("symbol", sort=False)[col].shift(lags)
        else:
            df[feat_name] = df[col].shift(lags)

        return df

    def _diff(
        self,
        df: pd.DataFrame,
        feat_name: str,
        step: Dict[str, Any]
    ) -> pd.DataFrame:
        """差分"""
        col = step.get("col")
        periods = step.get("periods", 1)
        per_symbol = step.get("per_symbol", True)

        if col not in df.columns:
            return df

        if per_symbol and "symbol" in df.columns:
            df[feat_name] = df.groupby("symbol", sort=False)[col].diff(periods)
        else:
            df[feat_name] = df[col].diff(periods)

        return df

    def _ema(
        self,
        df: pd.DataFrame,
        feat_name: str,
        step: Dict[str, Any]
    ) -> pd.DataFrame:
        """指数移动平均"""
        col = step.get("col")
        span = step.get("span", 10)
        per_symbol = step.get("per_symbol", True)

        if col not in df.columns:
            return df

        if per_symbol and "symbol" in df.columns:
            df[feat_name] = df.groupby("symbol", sort=False)[col].ewm(span=span, adjust=False).mean().reset_index(level=0, drop=True)
        else:
            df[feat_name] = df[col].ewm(span=span, adjust=False).mean()

        return df

    def _price_impact_hl(
        self,
        df: pd.DataFrame,
        feat_name: str,
        step: Dict[str, Any]
    ) -> pd.DataFrame:
        """价格冲击 (high-low 归一化)"""
        per_symbol = step.get("per_symbol", True)
        eps = step.get("eps", 1e-6)

        if "high" not in df.columns or "low" not in df.columns or "close" not in df.columns:
            return df

        if per_symbol and "symbol" in df.columns:
            close_shifted = df.groupby("symbol", sort=False)["close"].shift(1)
        else:
            close_shifted = df["close"].shift(1)

        df[feat_name] = (df["high"] - df["low"]) / (close_shifted + eps)

        return df


# ========== 全局单例 ==========
_default_runner = None

def get_runner(config_path: str = "feature_programs.json") -> FeatureProgramRunner:
    """获取全局特征程序运行器实例"""
    global _default_runner
    if _default_runner is None:
        _default_runner = FeatureProgramRunner(config_path)
    return _default_runner


def apply_feature_program(
    df: pd.DataFrame,
    program_id: str,
    verbose: bool = False
) -> Tuple[pd.DataFrame, List[str], Dict[str, Any]]:
    """
    便捷函数: 应用特征程序

    Args:
        df: 输入DataFrame
        program_id: 特征程序ID
        verbose: 是否打印详细日志

    Returns:
        (df_enriched, new_feature_cols, meta)
    """
    runner = get_runner()
    return runner.apply(df, program_id, verbose=verbose)
