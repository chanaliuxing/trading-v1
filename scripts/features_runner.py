#!/usr/bin/env python3
# features_runner.py
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
import json
import argparse
import sys
from pathlib import Path
import pandas as pd

# Add project root to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from libs.features.feature_dsl_v2 import build_features
from libs.features.feature_templates import expand_template

def load_programs(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    ap = argparse.ArgumentParser("features_runner")
    ap.add_argument("--input", required=True, help="CSV/Parquet path with columns: symbol, ts, open, high, low, close, volume, ret_1 ...")
    ap.add_argument("--output", required=True, help="Output CSV/Parquet path")
    ap.add_argument("--program-file", required=False, default="feature_programs.json")
    ap.add_argument("--program-id", required=False, help="Program id inside program-file")
    ap.add_argument("--template", required=False, help="Template name to expand (e.g., template.momentum)")
    ap.add_argument("--template-params", required=False, default="{}", help='JSON string for template kwargs')
    args = ap.parse_args()

    # 读取数据
    if args.input.lower().endswith(".parquet"):
        df = pd.read_parquet(args.input)
    else:
        df = pd.read_csv(args.input)

    # 选择程序
    if args.template:
        params = json.loads(args.template_params or "{}")
        program = expand_template(args.template, **params)
        programs = {"__from_template__": program}
        program_id = "__from_template__"
    else:
        programs = load_programs(args.program_file)
        if not args.program_id:
            raise SystemExit("When --program-file is used, --program-id must be provided.")
        program_id = args.program_id

    # 构建特征（零拷贝）
    df, new_cols = build_features(df, programs, program_id, allow_overwrite=False)
    print(f"[features_runner] new columns: {new_cols}")

    # 写出
    if args.output.lower().endswith(".parquet"):
        df.to_parquet(args.output, index=False)
    else:
        df.to_csv(args.output, index=False)

if __name__ == "__main__":
    main()
