#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test script for Feature DSL system

Tests the _apply_feature_program() method with sample data
"""

import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Mock minimal dependencies
class MockWalkForwardHPO:
    """Minimal mock to test _apply_feature_program() in isolation"""

    def _apply_feature_program(self, df: pd.DataFrame, program_id: str) -> pd.DataFrame:
        """
        Apply feature engineering program defined in feature_programs.json
        (Copied from wf_hpo.py for testing)
        """
        # Load feature programs
        try:
            with open("feature_programs.json", "r") as f:
                programs = json.load(f)
        except FileNotFoundError:
            print(f"[Test] feature_programs.json not found")
            return df
        except json.JSONDecodeError as e:
            print(f"[Test] Invalid feature_programs.json: {e}")
            return df

        # Get program
        if program_id not in programs:
            print(f"[Test] Unknown program_id '{program_id}'")
            return df

        program = programs[program_id]
        steps = program.get("steps", [])

        if not steps:
            print(f"[Test] Program '{program_id}' has no steps")
            return df

        print(f"\n[Test] Applying feature program '{program_id}': {program.get('description', '')}")
        print(f"[Test] {len(steps)} steps to execute")

        # Execute each step
        for i, step in enumerate(steps):
            feat_name = step.get("name")
            op = step.get("op")

            if not feat_name or not op:
                print(f"[Test] Step {i}: missing 'name' or 'op', skipping")
                continue

            try:
                # Ratio operators
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

                # Logarithm
                elif op == "log1p":
                    col = step.get("col")
                    if col in df.columns:
                        df[feat_name] = np.log1p(df[col].astype(float))

                # Rolling operators
                elif op == "rolling_sum":
                    col = step.get("col")
                    window = step.get("window_bars", 10)
                    min_periods = step.get("min_periods", 1)
                    per_symbol = step.get("per_symbol", True)

                    if col in df.columns:
                        if per_symbol and "symbol" in df.columns:
                            df[feat_name] = (
                                df.groupby("symbol", sort=False)[col]
                                .rolling(window=window, min_periods=min_periods)
                                .sum()
                                .reset_index(level=0, drop=True)
                            )
                        else:
                            df[feat_name] = df[col].rolling(window=window, min_periods=min_periods).sum()

                elif op == "rolling_mean":
                    col = step.get("col")
                    window = step.get("window_bars", 10)
                    min_periods = step.get("min_periods", 1)
                    per_symbol = step.get("per_symbol", True)

                    if col in df.columns:
                        if per_symbol and "symbol" in df.columns:
                            df[feat_name] = (
                                df.groupby("symbol", sort=False)[col]
                                .rolling(window=window, min_periods=min_periods)
                                .mean()
                                .reset_index(level=0, drop=True)
                            )
                        else:
                            df[feat_name] = df[col].rolling(window=window, min_periods=min_periods).mean()

                elif op == "rolling_std":
                    col = step.get("col")
                    window = step.get("window_bars", 20)
                    min_periods = step.get("min_periods", 5)
                    per_symbol = step.get("per_symbol", True)

                    if col in df.columns:
                        if per_symbol and "symbol" in df.columns:
                            df[feat_name] = (
                                df.groupby("symbol", sort=False)[col]
                                .rolling(window=window, min_periods=min_periods)
                                .std()
                                .reset_index(level=0, drop=True)
                            )
                        else:
                            df[feat_name] = df[col].rolling(window=window, min_periods=min_periods).std()

                elif op == "rolling_zscore":
                    col = step.get("col")
                    window = step.get("window_bars", 50)
                    min_periods = step.get("min_periods", 10)
                    per_symbol = step.get("per_symbol", True)

                    if col in df.columns:
                        if per_symbol and "symbol" in df.columns:
                            grouped = df.groupby("symbol", sort=False)[col]
                            roll_mean = grouped.rolling(window=window, min_periods=min_periods).mean()
                            roll_std = grouped.rolling(window=window, min_periods=min_periods).std().replace(0.0, 1.0)
                            df[feat_name] = ((df[col] - roll_mean) / roll_std).reset_index(level=0, drop=True)
                        else:
                            roll_mean = df[col].rolling(window=window, min_periods=min_periods).mean()
                            roll_std = df[col].rolling(window=window, min_periods=min_periods).std().replace(0.0, 1.0)
                            df[feat_name] = (df[col] - roll_mean) / roll_std

                # Microstructure operators
                elif op == "price_impact_hl":
                    per_symbol = step.get("per_symbol", True)
                    if "high" in df.columns and "low" in df.columns and "close" in df.columns:
                        if per_symbol and "symbol" in df.columns:
                            close_shifted = df.groupby("symbol", sort=False)["close"].shift(1)
                        else:
                            close_shifted = df["close"].shift(1)
                        df[feat_name] = (df["high"] - df["low"]) / (close_shifted + 1e-6)

                elif op == "effective_spread":
                    eps = step.get("eps", 1e-6)
                    if "close" in df.columns and "vwap" in df.columns:
                        df[feat_name] = 2 * np.abs(df["close"] - df["vwap"]) / (df["vwap"] + eps)

                elif op == "amihud_illiquidity":
                    eps = step.get("eps", 1e-9)
                    if "ret_1" in df.columns and "volume" in df.columns and "close" in df.columns:
                        df[feat_name] = np.abs(df["ret_1"]) / ((df["volume"] * df["close"]) + eps)

                else:
                    print(f"[Test] Step {i}: unknown operator '{op}', skipping")
                    continue

                print(f"[Test] Step {i+1}/{len(steps)}: created '{feat_name}' using '{op}' ✓")

            except Exception as e:
                print(f"[Test] Step {i}: error executing '{feat_name}' with '{op}': {e}")
                continue

        print(f"[Test] Feature program '{program_id}' completed\n")
        return df


def create_sample_data() -> pd.DataFrame:
    """Create sample DataFrame with TSLA data"""
    np.random.seed(42)

    n = 100
    ts = [datetime(2025, 10, 1, 9, 30) + timedelta(minutes=i) for i in range(n)]

    close = 250.0 + np.cumsum(np.random.randn(n) * 0.5)
    volume = np.random.randint(50000, 200000, size=n)

    df = pd.DataFrame({
        "ts": ts,
        "symbol": "TSLA",
        "open": close - np.random.rand(n) * 0.2,
        "high": close + np.random.rand(n) * 0.5,
        "low": close - np.random.rand(n) * 0.5,
        "close": close,
        "volume": volume,
        "vwap": close + np.random.randn(n) * 0.1,
        "atr_14": np.random.rand(n) * 2.0 + 1.0,
        "ret_1": np.random.randn(n) * 0.001,
        "ret_5": np.random.randn(n) * 0.002,
    })

    return df


def test_program(program_id: str):
    """Test a specific feature program"""
    print(f"\n{'='*70}")
    print(f"Testing program: {program_id}")
    print(f"{'='*70}")

    df = create_sample_data()
    print(f"[Test] Sample data shape: {df.shape}")
    print(f"[Test] Base columns: {list(df.columns)}")

    mock_hpo = MockWalkForwardHPO()
    df_result = mock_hpo._apply_feature_program(df, program_id)

    # Show new features
    new_cols = [c for c in df_result.columns if c not in df.columns]
    print(f"[Test] New features created: {len(new_cols)}")
    for col in new_cols:
        non_null = df_result[col].notna().sum()
        print(f"  - {col}: {non_null}/{len(df_result)} non-null values")

    # Show sample values
    if new_cols:
        print(f"\n[Test] Sample values from last 5 rows:")
        print(df_result[["symbol", "close"] + new_cols].tail())

    return df_result


def main():
    """Test all feature programs"""
    programs = [
        "none",
        "baseline_v1",
        "microstructure_v1",
        "momentum_v1",
        "mean_revert_v1",
        "volatility_v1",
        "comprehensive_v1"
    ]

    print("\n" + "="*70)
    print("Feature DSL Test Suite")
    print("="*70)

    results = {}
    for program_id in programs:
        try:
            df = test_program(program_id)
            results[program_id] = {
                "status": "✓ PASS",
                "features": len([c for c in df.columns if c not in create_sample_data().columns])
            }
        except Exception as e:
            results[program_id] = {
                "status": "✗ FAIL",
                "error": str(e)
            }
            print(f"\n[Test] ERROR: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print("\n" + "="*70)
    print("Test Summary")
    print("="*70)
    for program_id, result in results.items():
        status = result["status"]
        if "features" in result:
            print(f"{program_id:20s} {status:10s} ({result['features']} features)")
        else:
            print(f"{program_id:20s} {status:10s} ({result.get('error', 'Unknown error')})")

    print("\n" + "="*70)
    passed = sum(1 for r in results.values() if "✓" in r["status"])
    total = len(results)
    print(f"Result: {passed}/{total} programs passed")
    print("="*70)


if __name__ == "__main__":
    main()
